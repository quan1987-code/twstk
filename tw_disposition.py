# -*- coding: utf-8 -*-
r"""
處置股專區資料產生器（tw_disposition.py）v2
================================================================
產生 site/data/chuzhi.json 與 site/chuzhi.html。K 線下方「主力買賣超」副圖的基準資料
已由 build_site.py 用『三大法人合計』為每一檔股票填好（近一年），本程式只在處置相關個股
累積到足夠的『分點』資料時，把該檔的主力序列升級覆寫成更精準的分點主力（給 K 線副圖切換用）。
與 build_site.py / tw_volume_breakout_screener_v2.py 共用 twstock.db 與 FinMind 串接。

四狀態：watch(漲幅型估計) / confirmed(明日確定) / ongoing(處置中) / released(剛出關)
每檔指標（仿處置神器）：連次 連量 位階 月斜 累幅 剩天 主5 主10，主力買賣超日序列。

資料來源：TaiwanStockDispositionSecuritiesPeriod、TaiwanStockTradingDailyReport(分點,Sponsor)、twstock.db
用法：
  python tw_disposition.py             # 正常
  python tw_disposition.py --demo      # 離線示範
  python tw_disposition.py --no-chips  # 跳過分點(省流量)
"""
import os, sys, re, json, time, sqlite3, datetime, argparse
from statistics import pstdev
import pandas as pd
try:
    import tw_industry
except Exception:
    tw_industry = None
try:
    import requests
except Exception:
    requests = None
try:
    import yfinance as yf
except Exception:
    yf = None

# ===== CONFIG =====
DB_PATH = "twstock.db"
OUT_DIR = "site"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")
HTTP_TIMEOUT = 30
CHIP_MAX_STOCKS = 60          # 分點最多處理幾檔（控制首次回補時間）
CHIP_SLEEP = 0.25
CHIP_TIMEOUT = 45             # 分點(單日)請求逾時秒
MF_HISTORY_DAYS = 60          # 主力(分點)序列最多看回幾個交易日
MF_BACKFILL_CAP = 12          # 每檔每次最多補抓幾日（首次回補上限；之後每日約+1）
MF_OVERRIDE_MIN_DAYS = 20     # 分點主力序列至少累積這麼多天才覆寫 build_site 的三大法人主力基準
RELEASED_WINDOW_TD = 5
WATCH_CUM6_MIN = 25.0
K1_THRESHOLD = 32.0           # 注意股『漲幅型(第1款)』：近6日累積漲幅門檻 %（價格門檻）
VOL_MULT_K = 5.0             # 注意股『量能型』：當日量 ≥ 近60日均量 × 5（量價門檻）

DISP_COLS = {
    "stock_id": ["stock_id", "StockID", "stock_code"],
    "start":    ["start_date", "處置開始時間", "處置起日", "begin_date", "处置开始时间", "StartDate"],
    "end":      ["end_date", "處置結束時間", "處置迄日", "stop_date", "处置结束时间", "EndDate"],
    "announce": ["date", "Date", "公告日期"],
    "measure":  ["處置措施", "處置內容", "disposal_measures", "措施", "处置措施", "Disposal"],
}
CHIP_COLS = {
    "trader_id": ["securities_trader_id", "broker_id", "trader_id"],
    "trader":    ["securities_trader", "broker", "trader_name"],
    "buy":       ["buy", "buy_volume", "Buy"],
    "sell":      ["sell", "sell_volume", "Sell"],
    "date":      ["date", "Date"],
}

# ===== 小工具 =====
def pick_col(df, cands):
    if df is None or df.empty: return None
    s = set(df.columns)
    for c in cands:
        if c in s: return c
    return None

def looks_like_date(x):
    try: return bool(x) and len(str(x)) >= 8 and str(x)[4] in "-/"
    except Exception: return False

def detect_date_cols(df):
    out = []
    if df is None or df.empty: return out
    for c in df.columns:
        try: sample = df[c].dropna().astype(str).head(5).tolist()
        except Exception: continue
        if sample and all(looks_like_date(x) for x in sample): out.append(c)
    return out

def next_trading_day(today):
    try: d = datetime.date.fromisoformat(today)
    except Exception: d = datetime.date.today()
    d += datetime.timedelta(days=1)
    while d.weekday() >= 5: d += datetime.timedelta(days=1)
    return d.isoformat()

def biz_days_between(a, b):
    """a->b 之間的工作日數(不含 a、含 b；只跳週末，不含國定假日，近似)。a>b 回 0。"""
    try:
        da = datetime.date.fromisoformat(a[:10]); db = datetime.date.fromisoformat(b[:10])
    except Exception: return 0
    if db <= da: return 0
    n = 0; d = da
    while d < db:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5: n += 1
    return n

def _date_diff(a, b):
    try:
        return (datetime.date.fromisoformat(b[:10]) - datetime.date.fromisoformat(a[:10])).days
    except Exception: return 0

def now_taipei():
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")

# ===== 處置名單 =====
def parse_disposition(df, diag):
    if df is None or df.empty:
        diag["notes"].append("處置名單為空"); return []
    diag["disp_cols"] = list(df.columns)
    c_sid = pick_col(df, DISP_COLS["stock_id"]); c_start = pick_col(df, DISP_COLS["start"])
    c_end = pick_col(df, DISP_COLS["end"]); c_ann = pick_col(df, DISP_COLS["announce"])
    c_measure = pick_col(df, DISP_COLS["measure"])
    if not c_start or not c_end:
        dcols = [c for c in detect_date_cols(df) if c != c_ann]
        if len(dcols) >= 2:
            c_start = c_start or dcols[0]; c_end = c_end or dcols[1]
            diag["notes"].append(f"起迄日以樣式偵測：start={c_start}, end={c_end}")
        else:
            diag["notes"].append("找不到處置起迄日欄位（請看 disp_cols 補 DISP_COLS）")
    if not c_sid:
        diag["notes"].append("找不到 stock_id 欄位"); return []
    recs = []
    for _, row in df.iterrows():
        sid = str(row.get(c_sid, "")).strip()
        if not sid: continue
        start = str(row.get(c_start, "")).strip()[:10] if c_start else ""
        end = str(row.get(c_end, "")).strip()[:10] if c_end else ""
        measure = str(row.get(c_measure, "")).strip() if c_measure else ""
        method = "20分盤" if ("20" in measure or "二十" in measure) else ("5分盤" if ("5" in measure or "五" in measure) else "")
        rnd = 2 if any(k in measure for k in ("第二次","二次","第2次")) else (1 if any(k in measure for k in ("第一次","一次","第1次")) else "")
        recs.append({"sid": sid, "start": start, "end": end, "round": rnd, "method": method})
    by = {}
    for r in recs:
        k = r["sid"]
        if k not in by or (r["end"] or "") > (by[k]["end"] or ""): by[k] = r
    diag["disp_parsed"] = len(by)
    return list(by.values())

def categorize(disp_recs, today, universe, diag):
    """分成 ongoing/confirmed/released，並濾掉非個股(不在 universe 者，如權證)。"""
    ongoing, confirmed, released = [], [], []
    dropped = 0
    for r in disp_recs:
        sid = r["sid"]
        if universe and sid not in universe:
            dropped += 1; continue
        start, end = r.get("start",""), r.get("end","")
        if start and end and start <= today <= end:
            r2 = dict(r); r2["release"] = end; r2["d2r"] = biz_days_between(today, end)
            r2["day_total"] = biz_days_between(start, end) + 1
            r2["day_n"] = min(r2["day_total"], biz_days_between(start, today) + 1)
            ongoing.append(r2)
        elif start and start > today:
            r2 = dict(r); r2["days"] = biz_days_between(start, end) + 1 if end else None
            confirmed.append(r2)
        elif end and end < today:
            since = biz_days_between(end, today)
            if 0 < since <= RELEASED_WINDOW_TD + 1:
                r2 = dict(r); r2["since"] = since; released.append(r2)
    if dropped: diag["notes"].append(f"已濾除非個股 {dropped} 檔")
    confirmed.sort(key=lambda x: x.get("start",""))
    ongoing.sort(key=lambda x: x.get("d2r", 999))
    released.sort(key=lambda x: x.get("since", 999))
    return ongoing, confirmed, released

# ===== 價格指標(本地) =====
def load_window(con, ndays=70):
    dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM price ORDER BY date DESC LIMIT ?", (ndays,))]
    if not dates: return {}, ""
    dates = sorted(dates); qm = ",".join("?"*len(dates))
    rows = con.execute(f"SELECT stock_id,date,high,low,close,volume FROM price WHERE date IN ({qm})", dates).fetchall()
    by = {}
    for sid,d,h,l,c,v in rows: by.setdefault(sid, []).append((d,h,l,c,v))
    for sid in by: by[sid].sort(key=lambda x: x[0])
    return by, dates[-1]

def _ma(xs, n):
    if len(xs) < n: return None
    s = xs[-n:]
    s = [x for x in s if x is not None]
    return sum(s)/len(s) if s else None

def peak60_dist(seq, n=60):
    """目前收盤價 相對『近 n 日內「最高收盤價」那天的盤中最高價』的距離%。
    seq: [(date,high,low,close,volume) 由舊到新]。多數為負(收盤低於該峰高)。"""
    s = seq[-n:] if len(seq) > n else seq
    rows = [(r[1], r[3]) for r in s if r[3] is not None]   # (high, close)
    if not rows:
        return None
    peak_high, best_close = None, None
    for h, c in rows:
        if best_close is None or c > best_close:
            best_close, peak_high = c, h
    cur = rows[-1][1]
    if peak_high is None or peak_high <= 0 or cur is None:
        return None
    return round((cur / peak_high - 1) * 100, 1)

def compute_price_metrics(seq, idx6=0.0, disp_start=None):
    """seq: [(date,high,low,close,volume) 由舊到新]。回傳指標 dict。"""
    closes = [r[3] for r in seq if r[3] is not None]
    highs = [r[1] for r in seq if r[1] is not None]
    lows = [r[2] for r in seq if r[2] is not None]
    vols = [r[4] for r in seq if r[4] is not None]
    dates = [r[0] for r in seq if r[3] is not None]
    out = {"chg": None, "cum6": None, "lc": None, "ll": None, "wj": None, "yx": None, "lf": None,
           "ma20": None, "ma20_touch": False}
    if len(closes) < 2: return out
    last = closes[-1]; prev = closes[-2]
    out["chg"] = round((last/prev - 1)*100, 2) if prev else None
    # cum6
    if len(closes) >= 7 and closes[-7]:
        out["cum6"] = round((last/closes[-7]-1)*100, 1)
    # 連次：連續達第1款(每日 cum6>=32)
    lc = 0
    for i in range(len(closes)-1, 5, -1):
        base = closes[i-6]
        if base and (closes[i]/base-1)*100 >= K1_THRESHOLD: lc += 1
        else: break
    out["lc"] = lc
    # 連量：量比 = 今量 / 近20日均量 ×100
    if len(vols) >= 21:
        avg = sum(vols[-21:-1])/20.0
        out["ll"] = round(vols[-1]/avg*100, 0) if avg else None
    # 月斜：月線(MA20) 1日斜率%＝(今MA20−昨MA20)/昨MA20×100（>1%強勢、>3%妖股）
    ma20 = _ma(closes, 20); ma20_1 = _ma(closes[:-1], 20) if len(closes) >= 21 else None
    if ma20 and ma20_1: out["yx"] = round((ma20/ma20_1-1)*100, 2)
    # 當日K棒是否觸及20MA月線：今日最低 ≤ MA20 ≤ 今日最高
    if ma20:
        out["ma20"] = round(ma20, 2)
        thi, tlo = seq[-1][1], seq[-1][2]
        if thi is not None and tlo is not None and tlo <= ma20 <= thi:
            out["ma20_touch"] = True
    # 位階：20日布林通道整數級距。中線(MA20)=0、上軌(+2σ)=+10、下軌(-2σ)=-10，
    #       中線到上/下軌各分 10 等分，超出上下軌夾在 ±10。公式＝round((收盤-MA20)/(2σ)*10)。
    if len(closes) >= 20 and ma20:
        sd = pstdev(closes[-20:])
        if sd > 0:
            lvl = (last - ma20) / (2 * sd) * 10
            out["wj"] = int(max(-10, min(10, round(lvl))))
    # 累幅：(今收 − 處置前一日收) ÷ 處置前一日收 ×100；處置前一日＝處置起始日前最後一個交易日。
    if disp_start:
        sc = None
        for d, _, _, c, _ in seq:
            if c is None:
                continue
            if d < disp_start:
                sc = c          # 持續更新到處置起始日前最後一筆
            else:
                break
        out["lf"] = round((last / sc - 1) * 100, 1) if sc else out["cum6"]
    else:
        out["lf"] = out["cum6"]
    return out

def watch_estimate_days(cum6, lc):
    """最快可能進入處置的天數估計：處置約需累積 3 次漲幅型注意。
    今日已達注意門檻(cum6>=K1)時，最快 = max(1, 3 − 連續注意次數)；尚未達門檻則無法估(None)。"""
    if cum6 is None or cum6 < K1_THRESHOLD:
        return None
    return max(1, 3 - (lc or 0))

def vol_multiple(seq, n=60):
    """當日量 ÷ 近 n 日均量（含當日）。資料不足回 None。"""
    vols = [r[4] for r in seq if r[4] is not None]
    if len(vols) < 6:
        return None
    base = vols[-n:] if len(vols) >= n else vols
    avg = sum(base) / len(base)
    return round(vols[-1] / avg, 1) if avg > 0 else None

# ===== 分點：CC 與每日主力買賣超 =====
def cc_from_df(df):
    """對 df(可跨多日多券商) 算 (主力買賣超張, 集中度%)。"""
    if df is None or df.empty: return None, None
    cb = pick_col(df, CHIP_COLS["buy"]); cs = pick_col(df, CHIP_COLS["sell"])
    ct = pick_col(df, CHIP_COLS["trader_id"]) or pick_col(df, CHIP_COLS["trader"])
    if not cb or not cs: return None, None
    d = df.copy()
    d[cb] = pd.to_numeric(d[cb], errors="coerce").fillna(0)
    d[cs] = pd.to_numeric(d[cs], errors="coerce").fillna(0)
    g = d.groupby(ct, as_index=False)[[cb, cs]].sum() if ct else d
    g["net"] = g[cb] - g[cs]
    tb = g[cb].sum()
    if tb <= 0: return None, None
    pos = g[g["net"] > 0].nlargest(15, "net")["net"].sum()
    neg = g[g["net"] < 0].nsmallest(15, "net")["net"].sum()
    mf = (pos + neg)/1000.0
    cc = (pos - abs(neg))/tb*100.0
    return round(mf, 0), round(cc, 1)

def _date_col(df):
    return pick_col(df, CHIP_COLS["date"]) or (detect_date_cols(df)[0] if (df is not None and not df.empty) else None)

def daily_main_force(df, cd=None):
    """回傳 {date: 主力買賣超張}。"""
    if df is None or df.empty: return {}
    cd = cd or _date_col(df)
    if not cd: return {}
    out = {}
    for dt, sub in df.groupby(cd):
        mf, _ = cc_from_df(sub)
        if mf is not None: out[str(dt)[:10]] = mf
    return out

def cc_over_dates(df, date_set, cd=None):
    cd = cd or _date_col(df)
    if not cd: return None, None
    sub = df[df[cd].astype(str).str[:10].isin(date_set)]
    return cc_from_df(sub)

def trading_dates(con, n):
    ds = [r[0] for r in con.execute("SELECT DISTINCT date FROM price ORDER BY date DESC LIMIT ?", (n,))]
    return sorted(ds)

# 主力買賣超快取表（存進 twstock.db，跨次累積，避免每次重抓）
def ensure_mf_table(con):
    con.execute("CREATE TABLE IF NOT EXISTS mainforce(stock_id TEXT, date TEXT, mf REAL, PRIMARY KEY(stock_id,date))")
    con.commit()

def cached_mf_dates(con, sid):
    return set(r[0] for r in con.execute("SELECT date FROM mainforce WHERE stock_id=?", (sid,)))

def load_mf_series(con, sid, dates):
    if not dates: return {}
    qm = ",".join("?" * len(dates))
    rows = con.execute(f"SELECT date,mf FROM mainforce WHERE stock_id=? AND date IN ({qm}) AND mf IS NOT NULL",
                       [sid] + list(dates)).fetchall()
    return {d: m for d, m in rows}

def window_cc(ser, voln, dates):
    """主N = Σ(主力買賣超張) ÷ Σ(成交量張) ×100（等於各日集中度的量加權）。"""
    num = sum(ser[d] for d in dates if d in ser)
    den = sum(voln[d] for d in dates if d in ser and d in voln)
    return round(num / den * 100, 1) if den > 0 else None

# 分點『每券商每日淨買張』快取，供主5/主10 以區間彙總後取前15大買/賣方計算
def ensure_broker_table(con):
    con.execute("CREATE TABLE IF NOT EXISTS broker_net("
                "stock_id TEXT, date TEXT, trader TEXT, net REAL, PRIMARY KEY(stock_id,date,trader))")
    con.commit()

def broker_nets_from_df(df):
    """回傳 {券商: 當日淨買張}（買-賣，股數→張）。"""
    if df is None or df.empty:
        return {}
    cb = pick_col(df, CHIP_COLS["buy"]); cs = pick_col(df, CHIP_COLS["sell"])
    ct = pick_col(df, CHIP_COLS["trader_id"]) or pick_col(df, CHIP_COLS["trader"])
    if not cb or not cs or not ct:
        return {}
    d = df.copy()
    d[cb] = pd.to_numeric(d[cb], errors="coerce").fillna(0)
    d[cs] = pd.to_numeric(d[cs], errors="coerce").fillna(0)
    g = d.groupby(ct, as_index=False)[[cb, cs]].sum()
    out = {}
    for _, row in g.iterrows():
        net = (row[cb] - row[cs]) / 1000.0
        if net:
            out[str(row[ct])] = round(net, 2)
    return out

def store_broker_nets(con, sid, date, nets):
    if nets:
        con.executemany("INSERT OR REPLACE INTO broker_net VALUES (?,?,?,?)",
                        [(sid, date, t, n) for t, n in nets.items()])

def prune_broker_net(con, sid, keep_dates):
    """只保留窗內日期，避免 broker_net 無限長大。"""
    if not keep_dates:
        return
    con.execute("DELETE FROM broker_net WHERE stock_id=? AND date < ?", (sid, min(keep_dates)))

def window_concentration(con, sid, dates, vol_lots):
    """主N＝(前15大買方券商買超總和 − 前15大賣方券商賣超總和) ÷ 區間成交量(張) ×100。
    先把區間內各券商『淨買張』跨日彙總，再取前15大正值(買方)與前15大負值(賣方)。"""
    if not dates or not vol_lots or vol_lots <= 0:
        return None
    qm = ",".join("?" * len(dates))
    rows = con.execute(f"SELECT trader, SUM(net) FROM broker_net WHERE stock_id=? AND date IN ({qm}) "
                       f"GROUP BY trader", [sid] + list(dates)).fetchall()
    vals = [n for _, n in rows if n is not None]
    if not vals:
        return None
    pos15 = sum(sorted([n for n in vals if n > 0], reverse=True)[:15])   # 前15大買方買超總和
    neg15 = sum(sorted([n for n in vals if n < 0])[:15])                 # 前15大賣方賣超總和(負)
    return round((pos15 + neg15) / vol_lots * 100, 1)

def patch_stock_mf(out_dir, sid, mf_series):
    """用『分點』主力買賣超日序列覆寫 site/data/{sid}.json 的 mfs/mf。
    build_site 已先用三大法人合計為每一檔填好主力副圖基準；此處只在分點資料
    累積到 MF_OVERRIDE_MIN_DAYS 天以上時，才把處置相關個股升級成更精準的分點主力，
    避免用幾乎空白的分點序列蓋掉較完整的三大法人基準。但若該檔尚無基準
    （例如上櫃股 T86 尚未涵蓋），則有多少分點就先寫多少，總比沒有好。"""
    p = os.path.join(out_dir, "data", f"{sid}.json")
    if not os.path.exists(p) or not mf_series: return False
    try:
        with open(p, encoding="utf-8") as f: o = json.load(f)
    except Exception: return False
    d = o.get("d", [])
    if not d: return False
    base_span = (len(d) - o["mfs"]) if (o.get("mf") and o.get("mfs") is not None) else 0
    if base_span > 0 and len(mf_series) < MF_OVERRIDE_MIN_DAYS:
        return False   # 已有較完整的三大法人基準，分點還太少，先別覆寫
    smin = min(mf_series.keys())
    mfs = 0
    while mfs < len(d) and d[mfs] < smin: mfs += 1
    if mfs >= len(d): return False
    o["mfs"] = mfs
    o["mf"] = [round(mf_series.get(d[i], 0.0), 0) for i in range(mfs, len(d))]
    with open(p, "w", encoding="utf-8") as f:
        json.dump(o, f, ensure_ascii=False, separators=(",", ":"))
    return True

# ===== FinMind =====
def finmind_get(dataset, token, max_retry=4, timeout=None, **params):
    if requests is None: raise RuntimeError("requests 未安裝")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    q = {"dataset": dataset, **params}; wait = 8
    for _ in range(max_retry):
        try:
            resp = requests.get(FINMIND_URL, headers=headers, params=q, timeout=timeout or HTTP_TIMEOUT)
        except Exception as e:
            print(f"    [連線錯誤] {e}"); time.sleep(wait); wait = min(wait*2, 120); continue
        if resp.status_code in (402, 429):
            print(f"    [FinMind 流量上限] 等待 {wait}s"); time.sleep(wait); wait = min(wait*2, 120); continue
        if resp.status_code != 200:
            print(f"    [HTTP {resp.status_code}] {resp.text[:120]}"); return pd.DataFrame()
        return pd.DataFrame(resp.json().get("data", []))
    return pd.DataFrame()

# ===== 官方即時處置公告（TWSE / TPEx）=====
# 取代「等 FinMind 晚間(約21:00)批次」：官方 OpenAPI 在交易所公告後即到位，讓當日盤後(18:00)
# 建置就可能抓到「今天公告、明日起處置」的個股（→ 明日確定）。FinMind 仍作 fallback／回補起迄。
TWSE_PUNISH_URL = "https://openapi.twse.com.tw/v1/announcement/punish"          # 上市 處置有價證券
TPEX_DISPOSAL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"  # 上櫃 處置有價證券
_OFFICIAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# 欄位自適應候選鍵（政府 OpenAPI 中英欄名不一，先試已知鍵、再退回全欄位樣式偵測）
_SID_KEYS    = ("Code", "證券代號", "股票代號", "SecuritiesCompanyCode", "SecuritiesCode", "stock_id", "StockID")
_NAME_KEYS   = ("Name", "證券名稱", "股票名稱", "CompanyName", "SecuritiesName")
_START_KEYS  = ("處置開始時間", "處置起始日期", "處置開始日期", "StartDate", "start_date", "DispositionStartDate")
_END_KEYS    = ("處置結束時間", "處置結束日期", "EndDate", "end_date", "DispositionEndDate")
_PERIOD_KEYS = ("處置期間", "DispositionPeriod", "處置起訖", "處置期間及執行處置措施起訖時間", "Period")
_MEASURE_KEYS= ("處置措施", "處置內容", "DispositionMeasures", "Measure", "措施", "DispositionCondition", "處置條件")

def _iso_or_empty(y, mo, d):
    """組 ISO 並驗證月/日合理（月 1–12、日 1–31）；不合理回 ''（擋掉誤判成日期的雜訊數字）。"""
    if 1 <= mo <= 12 and 1 <= d <= 31 and 1990 <= y <= 2100:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""

def roc_any_to_iso(s):
    """民國/西元各種寫法 → ISO 'YYYY-MM-DD'。支援 '1130716'、'113/07/16'、'113.07.16'、
    '113年07月16日'、及西元 '2026-07-16'/'2026/07/16'。抓不到或月/日不合理回 ''。"""
    if s is None: return ""
    t = str(s).strip()
    if not t: return ""
    m = re.search(r"(19|20)(\d{2})[/.\-](\d{1,2})[/.\-](\d{1,2})", t)          # 西元
    if m:
        return _iso_or_empty(int(m.group(1)+m.group(2)), int(m.group(3)), int(m.group(4)))
    m = re.search(r"(\d{2,3})[年/.\-](\d{1,2})[月/.\-](\d{1,2})", t)            # 民國(帶分隔)
    if m:
        return _iso_or_empty(int(m.group(1))+1911, int(m.group(2)), int(m.group(3)))
    if t.isdigit() and len(t) == 7:                                            # 民國 7 碼 1130716
        return _iso_or_empty(int(t[:3])+1911, int(t[3:5]), int(t[5:7]))
    return ""

def _extract_two_dates(text):
    """從一段文字抓出前兩個(民國/西元)日期 → (start_iso, end_iso)；只一個回 (d,'')；無回 ('','')。"""
    if text is None: return "", ""
    found = []
    for m in re.finditer(r"(?:19|20)\d{2}[/.\-]\d{1,2}[/.\-]\d{1,2}"
                         r"|\d{2,3}[年/.\-]\d{1,2}[月/.\-]\d{1,2}|\b\d{7}\b", str(text)):
        iso = roc_any_to_iso(m.group(0))
        if iso and iso not in found:
            found.append(iso)
        if len(found) >= 2: break
    if len(found) >= 2: return found[0], found[1]
    if len(found) == 1: return found[0], ""
    return "", ""

def _pick_val(dic, keys):
    for k in keys:
        if k in dic and str(dic.get(k) or "").strip():
            return str(dic[k]).strip()
    return ""

def _parse_official_records(rows, market, diag, tag):
    """政府 OpenAPI list[dict] → recs [{sid,start,end,round,method,name,mkt}]。
    欄位自適應：先試已知鍵名，再退回全欄位樣式偵測；把實際欄位記到 diag 供首跑核對。"""
    if isinstance(rows, dict):                     # 少數端點外層包一層，取第一個 list
        rows = next((v for v in rows.values() if isinstance(v, list)), None)
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        diag["notes"].append(f"{tag}：無資料或格式非預期")
        return []
    diag.setdefault("official_cols", {})[tag] = list(rows[0].keys())
    recs, ok = [], 0
    for d in rows:
        sid = _pick_val(d, _SID_KEYS)
        if not re.fullmatch(r"\d{4,6}", sid or ""):        # 退回：任一值為 4~6 碼數字
            sid = next((str(v).strip() for v in d.values()
                        if re.fullmatch(r"\d{4,6}", str(v or "").strip())), "")
        if not re.fullmatch(r"\d{4,6}", sid or ""):
            continue
        start = roc_any_to_iso(_pick_val(d, _START_KEYS))
        end   = roc_any_to_iso(_pick_val(d, _END_KEYS))
        if not (start and end):                             # 起迄合在「處置期間」文字裡
            period = _pick_val(d, _PERIOD_KEYS)
            if not period:                                  # 再退回：掃所有值找含兩個日期者
                for v in d.values():
                    s2, e2 = _extract_two_dates(v)
                    if s2 and e2: period = str(v); break
            s2, e2 = _extract_two_dates(period)
            start, end = start or s2, end or e2
        measure = _pick_val(d, _MEASURE_KEYS)
        method = "20分盤" if ("20" in measure or "二十" in measure) else ("5分盤" if ("5" in measure or "五" in measure) else "")
        rnd = 2 if any(k in measure for k in ("第二次", "二次", "第2次")) else (1 if any(k in measure for k in ("第一次", "一次", "第1次")) else "")
        if start and end: ok += 1
        recs.append({"sid": sid, "start": start, "end": end, "round": rnd,
                     "method": method, "name": _pick_val(d, _NAME_KEYS), "mkt": market})
    diag["notes"].append(f"{tag}：{len(rows)} 列 → 解析 {len(recs)} 檔（起迄可辨識 {ok} 檔）")
    return recs

def fetch_official_disposition(diag):
    """TWSE + TPEx 官方處置公告（即時）。回傳合併 recs；任一來源失敗不影響另一個或主流程。"""
    if requests is None:
        diag["notes"].append("官方處置：requests 未安裝，略過"); return []
    sess = requests.Session()
    sess.headers.update({"User-Agent": _OFFICIAL_UA, "Accept": "application/json"})
    sess.verify = False   # 政府開放資料端點憑證在新版 OpenSSL 下驗證失敗，公開唯讀資料關閉驗證
    out = []
    for url, mkt, tag, params in (
            (TWSE_PUNISH_URL,   "上市", "TWSE處置", None),
            (TPEX_DISPOSAL_URL, "上櫃", "TPEx處置", {"l": "zh-tw"})):
        try:
            r = sess.get(url, params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            out += _parse_official_records(r.json(), mkt, diag, tag)
        except Exception as e:
            diag["notes"].append(f"{tag} 抓取失敗（改用 FinMind）：{e}")
    return out

def merge_disposition(finmind_recs, official_recs, diag):
    """以 FinMind（乾淨起迄）為底，官方即時源補『FinMind 尚無』的當日新公告；
    同一 sid 若官方 end 較新則更新起迄（處置延長／第二次）。回傳合併 recs。"""
    by = {r["sid"]: dict(r) for r in finmind_recs}
    added = updated = 0
    for r in official_recs:
        sid = r["sid"]
        if not (r.get("start") and r.get("end")):
            continue                      # 官方此筆起迄無法辨識 → 跳過（不污染；FinMind 有的話仍在）
        if sid not in by:
            by[sid] = dict(r); added += 1
        else:
            if (r.get("end") or "") > (by[sid].get("end") or ""):
                by[sid]["start"], by[sid]["end"] = r["start"], r["end"]; updated += 1
            for k in ("method", "round", "name"):
                if not by[sid].get(k) and r.get(k):
                    by[sid][k] = r[k]
    diag["notes"].append(f"官方即時源合併：新增 {added} 檔、更新 {updated} 檔（FinMind {len(finmind_recs)} 檔為底）")
    return list(by.values())

# ===== 通知：處置中個股達標即進通知 =====
def build_notifications(ongoing, today):
    """每次更新重新掃描處置中個股。進通知條件：月線斜率 yx > 1%，
    且（累計跌幅 lf ≤ -10/-20/-30%，或當日K棒觸及20MA月線）。跌幅越深排越前。"""
    out = []
    for r in ongoing:
        yx = r.get("yx")
        if yx is None or yx <= 1.0:
            continue
        lf = r.get("lf")
        touch = bool(r.get("ma20_touch"))
        tier = None
        if lf is not None:
            if lf <= -30: tier = -30
            elif lf <= -20: tier = -20
            elif lf <= -10: tier = -10
        if tier is None and not touch:
            continue
        reasons = []
        if tier is not None: reasons.append(f"累計跌幅破 {tier}%")
        if touch: reasons.append("觸及20MA月線")
        out.append({"sid": r["sid"], "name": r.get("name", ""), "mkt": r.get("mkt", ""),
                    "ind": r.get("ind", ""), "close": r.get("close"), "chg": r.get("chg"),
                    "yx": yx, "lf": lf, "wj": r.get("wj"), "ma20": r.get("ma20"),
                    "tier": tier, "touch": touch, "z5": r.get("z5"), "z10": r.get("z10"),
                    "reasons": reasons, "st": r.get("d2r"), "method": r.get("method"),
                    "round": r.get("round"), "day_n": r.get("day_n"), "day_total": r.get("day_total"),
                    "d2r": r.get("d2r"), "start": r.get("start"), "end": r.get("end"),
                    "pk60": r.get("pk60")})
    out.sort(key=lambda x: (x["tier"] if x["tier"] is not None else 0, -(x.get("yx") or 0)))
    return out

# ===== 組裝/輸出 =====
def build_payload(today, next_td, watch, confirmed, ongoing, diag):
    notify = build_notifications(ongoing, today)   # 通知掃描全部處置中（拆分前）
    # 依剩餘出關天數拆分：剩天(st/d2r) ≤3 → 即將出關(release_soon)；其餘(>3 或未知) → 處置中
    def _rem(r):
        v = r.get("st")
        return v if v is not None else r.get("d2r")
    release_soon = [r for r in ongoing if isinstance(_rem(r), (int, float)) and _rem(r) <= 3]
    ongoing_disp = [r for r in ongoing if not (isinstance(_rem(r), (int, float)) and _rem(r) <= 3)]
    # 附上概念股標籤(cpt)：供處置頁依概念分群（無概念時前端退回產業別 ind）
    try:
        import tw_concepts
        _cmap = tw_concepts.concept_map()
    except Exception:
        _cmap = {}
    for _lst in (watch, confirmed, ongoing_disp, release_soon, notify):
        for _r in _lst:
            _r["cpt"] = _cmap.get(str(_r.get("sid", "")), [])
    return {"gentime": now_taipei(), "today": today, "next_td": next_td,
            "counts": {"watch": len(watch), "confirmed": len(confirmed),
                       "ongoing": len(ongoing_disp), "release_soon": len(release_soon),
                       "notify": len(notify)},
            "diag": diag, "watch": watch, "confirmed": confirmed,
            "ongoing": ongoing_disp, "release_soon": release_soon, "notify": notify}

def write_outputs(out_dir, payload):
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
    with open(os.path.join(out_dir, "data", "chuzhi.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    build_v = "".join(ch for ch in (payload.get("gentime") or "") if ch.isdigit()) or "0"
    with open(os.path.join(out_dir, "chuzhi.html"), "w", encoding="utf-8") as f:
        f.write(CHUZHI_HTML.replace("__BUILDV__", build_v))
    c = payload["counts"]
    print(f"已寫出 {out_dir}/chuzhi.html 與 data/chuzhi.json "
          f"（可能進入處置 {c['watch']}・確定 {c['confirmed']}・處置中 {c['ongoing']}"
          f"・即將出關 {c.get('release_soon',0)}・通知 {c['notify']}）")

# ===== 示範資料 =====
def make_demo():
    today = "2026-06-27"
    diag = {"notes": ["[示範模式] 合成資料，非真實行情"], "disp_cols": []}
    def row(sid,name,mkt,close,chg,**kw):
        d = {"sid":sid,"name":name,"mkt":mkt,"close":close,"chg":chg}; d.update(kw); return d
    watch = [
        row("4129","聯合","上市",58.9,9.92,ind="生技-醫材",light="red",wj=8,yx=2.1,lf=33.8,vmult=6.4,st=1,pk60=-2.5,z5=11.2,z10=8.4),
        row("3083","網龍","上櫃",102.0,3.55,ind="電子下游-系統組裝",light="amber",wj=6,yx=1.4,lf=26.1,vmult=3.1,st=None,pk60=-8.0,z5=-3.4,z10=-1.1),
    ]
    confirmed = [
        row("2618","長榮航","上市",48.6,9.95,ind="航空",round=1,method="5分盤",start="2026-06-30",end="2026-07-11",
            wj=5,yx=1.6,lf=0.0,cum6=36.5,st=None,pk60=-5.0,z5=-6.4,z10=-5.2),
        row("6187","萬潤","上櫃",121.5,6.58,ind="半導體-設備",round=2,method="20分盤",start="2026-06-30",end="2026-07-13",
            wj=7,yx=2.4,lf=0.0,cum6=41.2,st=None,pk60=-3.1,z5=8.2,z10=5.5),
    ]
    ongoing = [
        # 剩天 >3 → 留在「處置中」
        row("2484","希華","上市",42.55,2.53,ind="電子上游-被動元件",round=1,method="5分盤",start="2026-06-23",end="2026-07-08",
            day_n=4,day_total=10,release="2026-07-08",d2r=5,wj=0,yx=1.6,lf=-14.0,ma20=44.0,ma20_touch=True,st=5,pk60=-18.2,z5=-6.4,z10=-5.2),
        # 剩天 ≤3 → 移到「即將出關」
        row("3339","泰谷","上市",59.8,-0.33,ind="光電-LED",round=1,method="5分盤",start="2026-06-23",end="2026-07-07",
            day_n=8,day_total=11,release="2026-07-07",d2r=2,wj=2,yx=1.9,lf=-23.0,ma20=61.0,ma20_touch=False,st=2,pk60=-24.1,z5=7.1,z10=1.3),
        row("8289","泰藝","上市",49.35,5.45,ind="電子上游-被動元件",round=2,method="20分盤",start="2026-06-23",end="2026-07-09",
            day_n=11,day_total=12,release="2026-07-09",d2r=1,wj=0,yx=1.5,lf=-31.0,ma20=50.2,ma20_touch=True,st=1,pk60=-30.4,z5=-6.2,z10=-7.2),
    ]
    return build_payload(today, next_trading_day(today), watch, confirmed, ongoing, diag)

# ===== 主程式 =====
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--no-chips", action="store_true")
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()
    if args.demo:
        write_outputs(args.out, make_demo()); return
    if not os.path.exists(DB_PATH):
        print(f"找不到 {DB_PATH}，改寫示範資料。"); write_outputs(args.out, make_demo()); return

    diag = {"notes": [], "disp_cols": []}
    con = sqlite3.connect(DB_PATH)
    names = {r[0]: r[1] for r in con.execute("SELECT stock_id,name FROM stock")}
    mkts = {r[0]: r[1] for r in con.execute("SELECT stock_id,market FROM stock")}
    universe = set(names.keys())

    win, today = load_window(con, 70)
    if not today: today = datetime.date.today().isoformat()
    next_td = next_trading_day(today)

    # 處置名單：官方即時源(TWSE+TPEx punish)為主 + FinMind 為底(乾淨起迄、fallback)。
    # 官方源讓「今天公告、明日起處置」的個股當日盤後就能抓到；FinMind 晚間批次僅作補漏。
    finmind_recs = []
    try:
        ds = (datetime.date.fromisoformat(today) - datetime.timedelta(days=45)).isoformat()
        df = finmind_get("TaiwanStockDispositionSecuritiesPeriod", FINMIND_TOKEN, start_date=ds)
        diag["notes"].append(f"FinMind 處置名單 {0 if df is None else len(df)} 筆")
        finmind_recs = parse_disposition(df, diag)
    except Exception as e:
        diag["notes"].append(f"FinMind 處置名單抓取失敗：{e}")
    try:
        official_recs = fetch_official_disposition(diag)
    except Exception as e:
        official_recs = []; diag["notes"].append(f"官方處置源整體失敗（改用 FinMind）：{e}")
    disp_recs = merge_disposition(finmind_recs, official_recs, diag)
    ongoing, confirmed, released = categorize(disp_recs, today, universe, diag)
    disp_sids = set(r["sid"] for r in disp_recs)

    # 大盤6日差幅
    idx6 = 0.0
    if yf is not None:
        try:
            h = yf.Ticker("^TWII").history(period="1mo")["Close"].dropna().tolist()
            if len(h) >= 7: idx6 = (h[-1]/h[-7]-1)*100
        except Exception: pass
    if idx6: diag["notes"].append(f"大盤6日差幅 {idx6:.1f}%")

    # 產業標籤（curated + FinMind 大分類快取）
    ind_map = {}
    if tw_industry is not None:
        try: ind_map = tw_industry.label_map(con)
        except Exception: ind_map = {}

    # 可能進入處置（漲幅型估計；含價格門檻 cum6 與量價門檻 量倍數）
    watch = []
    for sid, seq in win.items():
        if sid in disp_sids: continue
        if len(seq) < 7: continue
        m = compute_price_metrics(seq, idx6)
        if m["cum6"] is None or m["cum6"] < WATCH_CUM6_MIN: continue
        light = "red" if m["cum6"] >= K1_THRESHOLD else "amber"
        watch.append({"sid": sid, "name": names.get(sid,""), "mkt": mkts.get(sid,""),
                      "ind": ind_map.get(sid,""), "close": round(seq[-1][3],2), "chg": m["chg"],
                      "light": light, "wj": m["wj"], "yx": m["yx"], "lf": m["cum6"],
                      "vmult": vol_multiple(seq), "st": watch_estimate_days(m["cum6"], m["lc"]),
                      "pk60": peak60_dist(seq), "z5": None, "z10": None})
    watch.sort(key=lambda x: (0 if x["light"]=="red" else 1, -(x["lf"] or 0)))

    # 處置中／明日確定：補價格指標
    for lst in (ongoing, confirmed):
        for r in lst:
            seq = win.get(r["sid"], [])
            if seq:
                m = compute_price_metrics(seq, idx6, disp_start=r.get("start"))
                r["close"] = round(seq[-1][3], 2)
                r.update({"chg": m["chg"], "wj": m["wj"], "yx": m["yx"], "lf": m["lf"],
                          "cum6": m["cum6"], "ma20": m["ma20"], "ma20_touch": m["ma20_touch"],
                          "pk60": peak60_dist(seq)})
            r["name"] = names.get(r["sid"], r.get("name","")); r["mkt"] = mkts.get(r["sid"], "")
            r["ind"] = ind_map.get(r["sid"], "")
            r.setdefault("z5", None); r.setdefault("z10", None)
    # 剩天
    for r in ongoing: r["st"] = r.get("d2r")
    for r in confirmed: r["st"] = None

    # 分點：快取每券商每日淨買張(broker_net) 與每日主力淨買(mainforce)；只補抓近幾天(單日抓)。
    # 主5/主10 改用 broker_net 在 5/10 日窗內彙總後取前15大買/賣方計算。
    if not args.no_chips and FINMIND_TOKEN:
        ensure_mf_table(con); ensure_broker_table(con)
        targets = list(dict.fromkeys([r["sid"] for r in ongoing] + [r["sid"] for r in confirmed]
                                     + [r["sid"] for r in watch]))[:CHIP_MAX_STOCKS]
        mf_dates = trading_dates(con, MF_HISTORY_DAYS)
        idx_for = {r["sid"]: r for lst in (ongoing,confirmed,watch) for r in lst}
        fetched = 0; patched = 0; dbg_done = False
        for sid in targets:
            have = set(r[0] for r in con.execute(
                "SELECT DISTINCT date FROM broker_net WHERE stock_id=?", (sid,)))
            to_fetch = [d for d in mf_dates if d not in have][-MF_BACKFILL_CAP:]
            for d in to_fetch:
                try:
                    df = finmind_get("TaiwanStockTradingDailyReport", FINMIND_TOKEN, timeout=CHIP_TIMEOUT,
                                     data_id=sid, start_date=d, end_date=d)
                except Exception:
                    df = pd.DataFrame()
                if not dbg_done:
                    cols = list(df.columns) if (df is not None and not df.empty) else []
                    diag["notes"].append(f"分點首檔 {sid} {d}: rows={0 if df is None else len(df)} cols={cols}")
                    dbg_done = True
                mf, _ = cc_from_df(df)
                con.execute("INSERT OR REPLACE INTO mainforce VALUES (?,?,?)", (sid, d, mf))
                store_broker_nets(con, sid, d, broker_nets_from_df(df))
                fetched += 1
                time.sleep(CHIP_SLEEP)
            prune_broker_net(con, sid, mf_dates)
            con.commit()
            ser = load_mf_series(con, sid, mf_dates)
            if patch_stock_mf(args.out, sid, ser): patched += 1
            seq = win.get(sid, [])
            voln = {dd: (v/1000.0) for dd,_,_,_,v in seq if v}
            r = idx_for.get(sid)
            if r is not None:
                vol5 = sum(voln.get(d, 0) for d in mf_dates[-5:])
                vol10 = sum(voln.get(d, 0) for d in mf_dates[-10:])
                # 主5/主10：優先用 broker_net 區間彙總(前15大買-賣方)；broker_net 尚未累積到位時，
                # 退回用 mainforce 每日主力淨買的量加權(window_cc)，避免顯示「—」。
                z5 = window_concentration(con, sid, mf_dates[-5:], vol5)
                z10 = window_concentration(con, sid, mf_dates[-10:], vol10)
                if z5 is None: z5 = window_cc(ser, voln, mf_dates[-5:])
                if z10 is None: z10 = window_cc(ser, voln, mf_dates[-10:])
                r["z5"] = z5; r["z10"] = z10
        diag["notes"].append(f"分點：本次抓 {fetched} 日，升級為分點主力 {patched}/{len(targets)} 檔"
                             f"（未達 {MF_OVERRIDE_MIN_DAYS} 天者沿用三大法人主力基準；快取累積中）")
    con.close()
    write_outputs(args.out, build_payload(today, next_td, watch, confirmed, ongoing, diag))

CHUZHI_HTML = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#000000">
<title>處置股專區 ・ 台股看板</title>
<link rel="manifest" href="manifest.json">
<style>
  :root{
    --bg:#000000; --card:#121214; --card2:#1b1b1f; --border:#2a2a2f;
    --text:#f0f1f3; --muted:#9a9aa2; --dim:#67676e;
    --amber:#ffcf3a; --amber-s:rgba(255,207,58,.15);
    --up:#fb3b41; --down:#1ec77a;
    --blue:#5aa9ff; --blue-s:rgba(90,169,255,.12);
    --purple:#b794ff; --purple-s:rgba(183,148,255,.12);
    --red-s:rgba(251,59,65,.14); --grn-s:rgba(30,199,122,.13);
  }
  *{box-sizing:border-box;}
  body{margin:0; background:var(--bg); color:var(--text);
    font-family:system-ui,-apple-system,"PingFang TC","Noto Sans TC","Microsoft JhengHei","Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased; padding:16px 12px 40px; padding-top:calc(16px + env(safe-area-inset-top));}
  .num{font-variant-numeric:tabular-nums;}
  .wrap{max-width:1180px; margin:0 auto;}
  a{color:var(--blue); text-decoration:none;}
  header h1{font-size:19px; font-weight:800; margin:0;}
  .sub{font-size:12px; color:var(--muted); margin-top:5px; line-height:1.5;}
  .hidden{display:none !important;}

  .cztabs{display:flex; gap:8px; margin:12px 0; background:transparent; padding:2px 0; border-bottom:1px solid var(--border);
    overflow-x:auto; -webkit-overflow-scrolling:touch; scrollbar-width:none;}
  .cztabs::-webkit-scrollbar{display:none;}
  .czt{flex:0 0 auto; background:transparent; color:var(--muted); border:none; border-radius:99px;
    padding:8px 14px; font-size:13.5px; font-weight:700; cursor:pointer; white-space:nowrap;}
  .czt.on{background:var(--amber); color:#000;}

  .pane{animation:fade .2s ease;}
  @keyframes fade{from{opacity:0; transform:translateY(4px);}to{opacity:1; transform:none;}}

  .stats{display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-bottom:14px;}
  .stat{background:var(--card); border:1px solid var(--border); border-radius:11px; padding:12px 10px; text-align:center;}
  .stat .n{font-size:24px; font-weight:800; line-height:1;}
  .stat .l{font-size:11px; color:var(--muted); margin-top:6px;}
  .stat.w .n{color:var(--amber);} .stat.c .n{color:var(--up);}
  .stat.o .n{color:var(--blue);} .stat.r .n{color:var(--down);}

  .sech{font-size:13px; font-weight:700; color:var(--muted); margin:18px 2px 9px; display:flex; align-items:center; gap:7px;}
  .sech .pill{font-size:11px; font-weight:600; color:var(--dim); background:var(--card2); border:1px solid var(--border); padding:2px 8px; border-radius:99px;}

  .card{background:var(--card); border:1px solid var(--border); border-radius:9px; padding:12px 14px; margin-bottom:9px;}
  .card .top{display:flex; align-items:flex-start; gap:9px;}
  .card .lhs{flex:1; min-width:0; cursor:pointer;}
  .card .sid{font-size:16px; font-weight:800; color:var(--text); font-variant-numeric:tabular-nums;}
  .card .nm{font-size:14px; color:var(--blue); margin-left:7px;}
  .card .lhs:active .nm{opacity:.6;}
  .card .period{font-size:11px; color:var(--dim); margin-top:3px; font-variant-numeric:tabular-nums;}
  .card .cind{font-size:10.5px; font-weight:600; color:#7c8aa0; margin-top:2px; letter-spacing:.2px;}
  .card .mkt{font-size:10.5px; color:var(--dim); border:1px solid var(--border); border-radius:5px; padding:1px 6px; margin-left:6px;}
  .card .px{text-align:right; flex:none;}
  .card .px .p{font-size:16px; font-weight:800; font-variant-numeric:tabular-nums;}
  .card .px .c{font-size:12.5px; font-weight:700; font-variant-numeric:tabular-nums;}

  .mgrid{display:grid; grid-template-columns:repeat(2,1fr); gap:7px 6px; margin-top:11px;}
  .mcell{background:var(--card2); border-radius:8px; padding:7px 8px; line-height:1.32;}
  .mcell .mr{display:flex; justify-content:space-between; align-items:baseline; gap:4px;}
  .mcell .ml{font-size:10px; color:var(--dim); font-weight:600;}
  .mcell .mv{font-size:13.5px; font-weight:800; font-variant-numeric:tabular-nums;}
  .mcell .mv.sm{font-size:12px;}

  /* 緊湊表格（仿處置神器；凍結首欄、可左右滑動、欄位標題排序） */
  .tblhint{font-size:11px; color:var(--dim); margin:2px 2px 8px; line-height:1.5;}
  /* 同時可垂直＋水平捲動的盒：向下滑動時表頭(sticky top)固定、向右滑動時首欄(sticky left)固定 */
  .dtbl-wrap{overflow:auto; max-height:74vh; -webkit-overflow-scrolling:touch; border:1px solid var(--border); border-radius:11px; background:var(--card); overscroll-behavior:contain;}
  .dtbl{border-collapse:separate; border-spacing:0; width:max-content; min-width:100%; font-variant-numeric:tabular-nums;}
  .dtbl th,.dtbl td{padding:7px 11px; text-align:right; white-space:nowrap; border-bottom:1px solid var(--border);}
  .dtbl tbody tr:last-child td{border-bottom:none;}
  .dtbl thead th{position:sticky; top:0; z-index:3; background:var(--card2); color:var(--muted); font-size:11px; font-weight:700; line-height:1.55;}
  .dtbl th.frz,.dtbl td.frz{position:sticky; left:0; z-index:2; text-align:left; background:var(--card);}
  .dtbl thead th.frz{z-index:4; background:var(--card2); box-shadow:1px 0 0 var(--border);}
  .dtbl td.frz{box-shadow:1px 0 0 var(--border);}
  .dtbl .sortlbl{cursor:pointer; display:inline-block; padding:1px 3px; border-radius:5px;}
  .dtbl .sortlbl.on{color:var(--amber); background:var(--amber-s);}
  .dtbl .sortlbl i{font-style:normal; font-size:9px; margin-left:1px;}
  /* 概念分群：切換鈕 + 群組標題列 */
  .gtog{background:var(--card); color:var(--muted); border:1px solid var(--border); border-radius:8px; padding:4px 10px; font-size:12px; cursor:pointer; font-weight:700; vertical-align:middle;}
  .gtog.on{background:var(--amber-s); color:var(--amber); border-color:rgba(245,165,36,.4);}
  .dtbl tr.grouphdr td{text-align:left; background:var(--card2); border-top:2px solid var(--border); padding:6px 11px; font-weight:800; font-size:13px; color:var(--text);}
  .dtbl tr.grouphdr .ghlbl{position:sticky; left:10px; display:inline-block;}
  .dtbl tr.grouphdr .gchip{display:inline-block; font-size:10px; font-weight:700; padding:1px 6px; border-radius:5px; margin-right:7px;}
  .dtbl tr.grouphdr.gc .gchip{background:rgba(77,159,255,.16); color:#6fb0ff;}
  .dtbl tr.grouphdr.gi .gchip{background:rgba(94,111,134,.18); color:#93a3b8;}
  .dtbl tr.grouphdr .gcount{color:var(--dim); font-weight:600; font-size:11px; margin-left:6px;}
  .dtbl tbody tr{cursor:pointer;}
  .dtbl tbody tr:active{background:rgba(255,255,255,.05);}
  .dtbl tbody tr:active td.frz{background:#10192b;}
  .dtbl .cv{font-weight:800; font-size:13px;}
  .dtbl .nmcell{min-width:118px;}
  .dtbl .nmcell .nm{font-weight:700; font-size:14px; color:var(--text);}
  .dtbl .nmcell .sub{font-size:10.5px; color:var(--dim); margin-top:1px;}
  .dtbl .nmcell .cind{font-size:10px; color:#7c8aa0; font-weight:600; margin-top:1px;}
  .dtbl .nmcell .per{font-size:10px; color:var(--dim); margin-top:1px; font-variant-numeric:tabular-nums;}
  .dtbl tr.side-up td.frz{box-shadow:inset 3px 0 0 var(--up), 1px 0 0 var(--border);}
  .dtbl tr.side-down td.frz{box-shadow:inset 3px 0 0 var(--down), 1px 0 0 var(--border);}
  /* 通知卡 */
  .ncard{border-color:rgba(245,165,36,.35);}
  .ncard.t20{border-color:rgba(255,77,79,.45);}
  .ncard.t30{border-color:rgba(255,77,79,.7); box-shadow:0 0 0 1px rgba(255,77,79,.25) inset;}
  .nreasons{display:flex; flex-wrap:wrap; gap:6px; margin-top:9px;}
  .ntag{font-size:11px; font-weight:700; color:#ffd9da; background:var(--red-s); border:1px solid rgba(255,77,79,.3); border-radius:7px; padding:3px 8px;}
  .ntag.yx{color:var(--amber); background:var(--amber-s); border-color:rgba(245,165,36,.3);}
  .ntag.ma{color:#9fd0ff; background:rgba(77,159,255,.12); border-color:rgba(77,159,255,.3);}
  /* 通知表格名稱欄內的觸發原因標籤 */
  .nmcell .nrz{display:flex; flex-wrap:wrap; gap:3px; margin-top:4px; max-width:180px; white-space:normal;}
  .nmcell .nrz .ntag{font-size:9.5px; font-weight:700; padding:1px 5px; border-radius:6px;}

  .up{color:var(--up);} .down{color:var(--down);} .flat{color:var(--muted);} .amb{color:var(--amber);}
  .dot{display:inline-block; width:9px; height:9px; border-radius:99px; margin-right:2px; vertical-align:middle;}
  .dot.red{background:var(--up);}
  .dot.amber{background:var(--amber);}
  .dot.green{background:var(--down);}

  .prog{height:6px; background:#000; border:1px solid var(--border); border-radius:5px; overflow:hidden; flex:1;}
  .progf{height:100%; background:var(--amber); border-radius:5px;}
  .progline{display:flex; align-items:center; gap:9px; margin-top:10px;}
  .progline .pt{font-size:11px; color:var(--muted); font-variant-numeric:tabular-nums; white-space:nowrap;}

  .chip{display:inline-block; font-size:10.5px; font-weight:700; padding:1px 7px; border-radius:5px;}
  .chip.m5{background:var(--red-s); color:var(--up);} .chip.m20{background:var(--purple-s); color:var(--purple);}
  .chip.r2{background:var(--red-s); color:var(--up); margin-left:5px;}

  .empty{color:var(--dim); font-size:13.5px; text-align:center; padding:34px 12px; line-height:1.7;}
  .note{font-size:12px; color:var(--dim); line-height:1.65; margin-top:14px; padding:13px 15px;
    background:var(--card); border:1px solid var(--border); border-radius:11px;}
  .note b{color:var(--muted);}

  details.expl{margin-top:14px; background:var(--card); border:1px solid var(--border); border-radius:11px; overflow:hidden;}
  details.expl summary{padding:13px 15px; font-size:13px; font-weight:700; color:var(--amber); cursor:pointer; list-style:none;}
  details.expl summary::-webkit-details-marker{display:none;}
  details.expl summary::before{content:"\25B8  "; color:var(--amber);}
  details.expl[open] summary::before{content:"\25BE  ";}
  .expbody{padding:0 15px 14px; font-size:12.5px; line-height:1.7; color:var(--muted);}
  .expbody b{color:var(--text);}
  .expbody .g{display:grid; grid-template-columns:auto 1fr; gap:5px 11px; margin-top:6px;}
  .expbody .k{color:var(--amber); font-weight:700; white-space:nowrap;}

  .doc{background:var(--card); border:1px solid var(--border); border-radius:9px; padding:17px 18px; line-height:1.72; font-size:14px;}
  .doc h3{font-size:15.5px; margin:20px 0 9px; color:var(--amber);}
  .doc h3:first-child{margin-top:2px;}
  .doc p{margin:8px 0; color:var(--text);}
  .doc .lead{color:var(--muted); font-size:13.5px;}
  .doc ul{margin:8px 0; padding-left:20px;} .doc li{margin:6px 0;}
  .doc .k{color:var(--amber); font-weight:700;}
  .doc .warn{background:var(--red-s); border:1px solid rgba(255,77,79,.3); border-radius:9px; padding:11px 13px; margin:12px 0; font-size:13px; color:#ffd9da;}
  .doc .step{display:flex; gap:11px; margin:11px 0;}
  .doc .step .no{flex:0 0 26px; height:26px; border-radius:99px; background:var(--amber-s); color:var(--amber); font-weight:800; font-size:13px; display:flex; align-items:center; justify-content:center;}
  .doc .step .tx{flex:1;}
  .doc table{width:100%; border-collapse:collapse; margin:12px 0; font-size:12.5px;}
  .doc th,.doc td{border:1px solid var(--border); padding:8px 9px; text-align:left; vertical-align:top;}
  .doc th{background:var(--card2); color:var(--muted); font-weight:700;}
  .discl{font-size:11.5px; color:var(--dim); margin-top:16px; line-height:1.6;}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>🚦 處置股專區</h1>
    <div class="sub">資料日 <span id="today" class="num">—</span> ・ 更新 <span id="gentime" class="num">—</span>（台北）　<button class="gtog on" id="dispGtog" title="依概念股/產業族群分組排列">☰ 依概念分群</button>　<a href="index.html">← 回主看板</a>　<a href="hui.html">🐉 輝哥選股</a>　<a href="market.html">市場分析</a></div>
  </header>

  <div class="cztabs" id="cztabs">
    <button class="czt on" data-p="ov">總覽</button>
    <button class="czt" data-p="notify">🔔 通知</button>
    <button class="czt" data-p="watch">可能進入處置</button>
    <button class="czt" data-p="confirmed">📌 明日確定</button>
    <button class="czt" data-p="ongoing">處置中</button>
    <button class="czt" data-p="release">🔓 即將出關</button>
    <button class="czt" data-p="teach">實戰教學</button>
    <button class="czt" data-p="rule">規則說明</button>
  </div>

  <div class="pane" id="p-ov">
    <div class="stats">
      <div class="stat r"><div class="n num" id="cnt-n">—</div><div class="l">🔔 通知</div></div>
      <div class="stat w"><div class="n num" id="cnt-w">—</div><div class="l">可能進入處置</div></div>
      <div class="stat"><div class="n num" id="cnt-c" style="color:var(--blue)">—</div><div class="l">📌 明日確定</div></div>
      <div class="stat o"><div class="n num" id="cnt-o">—</div><div class="l">處置中</div></div>
      <div class="stat"><div class="n num" id="cnt-rs" style="color:var(--down)">—</div><div class="l">🔓 即將出關</div></div>
    </div>
    <div class="note">
      <b>燈號</b>：<span class="dot red"></span>紅＝今日已觸發漲幅型（第1款）　<span class="dot amber"></span>黃＝接近門檻　<span class="dot green"></span>綠＝安全。<br>
      點任何個股的<b style="color:var(--blue)">列</b>可跳到該股 K 線圖（K 線下方副圖可切換 MACD／主力買賣超）。清單為<b>緊湊表格</b>：可左右滑動看更多指標、點欄位標題排序。股名下方小字為<b>產業類型</b>。分點資料：FinMind Sponsor（T+1 盤後）。
    </div>
    <div class="note" id="diagbox" style="display:none"></div>
  </div>

  <div class="pane hidden" id="p-notify">
    <div class="sech">🔔 通知（每次更新重新掃描） <span class="pill">處置中・月斜&gt;1%・跌幅破關或觸月線</span></div>
    <div class="tblhint">與「處置中」相同排版・點欄位標題排序・左右滑動看更多指標・點列看 K 線</div>
    <div id="list-notify"></div>
    <div id="expl-notify"></div>
  </div>

  <div class="pane hidden" id="p-watch">
    <div class="sech">📈 可能進入處置 <span class="pill">注意/處置門檻・盤後自算</span></div>
    <div class="tblhint">點欄位標題排序（再點切換升/降冪）・表格可左右滑動看更多指標・點列看 K 線</div>
    <div id="list-watch"></div>
    <div id="expl-watch"></div>
  </div>

  <div class="pane hidden" id="p-confirmed">
    <div class="sech">📌 明日確定 <span class="pill">交易所已公告・下一交易日起處置</span></div>
    <div class="tblhint">已公告、處置尚未開始（隔日起分盤）・點欄位標題排序・左右滑動看更多指標・點列看 K 線</div>
    <div id="list-confirmed"></div>
    <div id="expl-confirmed"></div>
  </div>

  <div class="pane hidden" id="p-ongoing">
    <div class="sech">⛓️ 處置中（坐牢） <span class="pill">分盤交易期・剩餘 &gt;3 交易日</span></div>
    <div class="tblhint">點欄位標題排序・左右滑動看更多指標・點列看 K 線（剩餘 ≤3 日者見「即將出關」）</div>
    <div id="list-ongoing"></div>
    <div id="expl-ongoing"></div>
  </div>

  <div class="pane hidden" id="p-release">
    <div class="sech">🔓 即將出關 <span class="pill">剩餘 ≤3 交易日・分盤即將解除</span></div>
    <div class="tblhint">由「處置中」自動移入（剩餘出關天數 ≤3 日）・排版與處置中相同・點欄位標題排序・點列看 K 線</div>
    <div id="list-release"></div>
    <div id="expl-release"></div>
  </div>

  <div class="pane hidden" id="p-teach">
    <div class="doc">
      <h3>一句話：處置股在玩什麼？</h3>
      <p class="lead">股票短期漲太兇（或量、當沖太誇張）會被交易所「關起來」分盤交易、限制當沖。流動性被抽乾、籌碼鎖死，股價容易<b>暴漲暴跌</b>。我們不賭它被關，而是用本站指標，在「處置中被錯殺」時找低接、並嚴設停損。</p>

      <h3>核心買點</h3>
      <div class="step"><div class="no">1</div><div class="tx"><b>處置後跌深＝大買機會</b>：處置中個股自處置起算<b>累幅跌 20% 以上</b>（坐牢被殺過頭），常是恐慌錯殺、易反彈的大買點。跌破 -10%／-20%／-30% 會自動進「🔔 通知」。</div></div>
      <div class="step"><div class="no">2</div><div class="tx"><b>浪子回頭（跟位階有關）</b>：強勢股拉回月線（20MA）止跌、量縮、主力沒走。要件＝<b>月斜為正</b>、<b>主5／主10 為正</b>（主力沒倒貨）、<b>位階偏低</b>（接近下軌 −10 最佳）、價格貼著月線。位階越低、拉回越深越安全。</div></div>
      <div class="step"><div class="no">3</div><div class="tx"><b>低檔爆大量＝底部訊號</b>：位階低（基期低）時突然<b>爆出大量</b>，常是主力進場、打底訊號，可留意。</div></div>
      <div class="step"><div class="no">4</div><div class="tx"><b>不破低點＋出紅K＝加碼點</b>：回測前低不破、收一根<b>實體紅K</b>，可考慮加碼。</div></div>

      <h3>用本站指標選股（怎麼按）</h3>
      <ul>
        <li><span class="k">位階：低 → 高 排序</span>：點「位階」欄由小到大排，<b>從低位階（−10 附近）開始找</b>低接標的。</li>
        <li><span class="k">月斜：只要正的</span>：月線斜率為正（&gt;1% 強勢）才看；<b>斜率為負的直接跳過</b>。</li>
        <li><span class="k">主5／主10：要正的</span>：代表主力仍站在買方、沒在倒貨。</li>
      </ul>

      <h3>主力買盤強度（主5／主10 怎麼看）</h3>
      <table>
        <tr><th>主5／主10（集中度%）</th><th>解讀</th></tr>
        <tr><td><b>0 ~ 10</b></td><td>小買</td></tr>
        <tr><td><b>10 ~ 20</b></td><td>中買</td></tr>
        <tr><td><b>20 以上</b></td><td>大買</td></tr>
      </table>
      <p class="lead">處置股的主5／主10 <b>都是正的就不錯</b>（主力仍在買方）。轉負＝主力派發，警訊。</p>

      <h3>停利</h3>
      <p><span class="k">賺 10% 先賣一些</span>：獲利約 +10% 就<b>分批減碼</b>落袋，留部位續抱。</p>

      <h3>停損鐵則</h3>
      <div class="warn">
        ① <b>收盤跌破月線(MA20) 超過 3%</b>，或<b>連續 2 個交易日收盤站不回月線</b> → 出。<br>
        ② <b>主5 或主10（CC）轉為負值</b>，或<b>前 15 大買超券商分點（主力買賣超）出現集體撤退、大舉倒貨</b> → 即使價格還沒跌破月線，也代表支撐虛弱，必須<b>提早甚至立即出清停損</b>。
      </div>

      <h3>操作禁忌</h3>
      <ul>
        <li><b>跌停不要買</b>（分盤跌停易持續鎖死、流動性差）。</li>
        <li><b>漲停不要賣</b>（強勢鎖漲停，賣了常少賺一大段）。</li>
        <li>處置期間<b>禁現股／資券當沖</b>；當沖税費＋分盤滑價常吃光價差。</li>
      </ul>
      <p class="discl">本頁為交易紀律整理，非投資建議。門檻數字需依你自己的回測校準。</p>
    </div>
  </div>

  <div class="pane hidden" id="p-rule">
    <div class="doc">
      <h3>注意股 vs 處置股</h3>
      <p><b>注意股</b>：盤後計算，達標就公告，只是提醒、交易方式不變。<b>處置股</b>：注意累積到一定次數後升級，真的有交易限制（改分盤、要預收錢、禁當沖）。先注意、再處置。</p>
      <h3>怎樣會被「注意」？</h3>
      <ul>
        <li><span class="k">漲太快</span>：近6個交易日累積漲跌幅 <b>超過32%</b>（且明顯比大盤、同類股強）；或超過25%且這6天頭尾價差達50元（多為高價股）。</li>
        <li><span class="k">量爆掉</span>：當天量是近60日均量5倍以上。</li>
        <li><span class="k">週轉率太高</span>：當天＞10%、或近6日累積＞50%。</li>
        <li><span class="k">當沖太兇</span>：近6日與當日當沖佔比都＞60%（會讓處置拉長到12天）。</li>
      </ul>
      <h3>怎樣會被「處置」？</h3>
      <p>近期累積到門檻就升級：<b>連續3天</b>達漲幅型；或<b>連5天／近10天6天／近30天12天</b>達前述任一款。</p>
      <h3>第一次 vs 第二次</h3>
      <table>
        <tr><th></th><th>第一次</th><th>第二次以上</th></tr>
        <tr><td>撮合</td><td>約每 <b>5分鐘</b></td><td>約每 <b>20分鐘</b></td></tr>
        <tr><td>預收</td><td>單筆≥10張或累積≥30張</td><td><b>不論張數全額預收</b></td></tr>
        <tr><td>信用</td><td>視個案</td><td>停融資、融券保證金100%</td></tr>
        <tr><td>天數</td><td>10天（當沖太兇→12天）</td><td>10~12天</td></tr>
      </table>
      <h3>處置期間限制</h3>
      <ul><li><b>不能現股／資券當沖</b>。</li><li>只能掛<b>限價單</b>，分盤集合競價，每5秒揭示模擬價量與五檔。</li><li>對應的<b>股票期貨不受限</b>（保證金調高）。</li></ul>
      <h3>上市／上櫃／興櫃</h3>
      <p>上市與上櫃標準幾乎一樣。<b>興櫃完全不同</b>：無漲跌幅、議價，均價較前一日差50%就熔斷停到收盤，波動極端。</p>
      <p class="discl">以上為簡化說明，量化門檻以證交所與櫃買中心最新公告為準。</p>
    </div>
  </div>
</div>
<footer style="text-align:center; color:var(--dim); font-size:11px; padding:16px 14px 30px; border-top:1px solid var(--border); line-height:1.6">資料來源：台灣證交所／櫃買中心 OpenAPI（處置公告・即時）、<a href="https://finmindtrade.com" target="_blank" rel="noopener" style="color:var(--blue); text-decoration:none">FinMind</a>（處置起迄回補／分點籌碼／價量・法人） ・ 僅供研究，非投資建議</footer>

<script>
const BUILD_V = "__BUILDV__" || "0";
const $ = id => document.getElementById(id);
const esc = s => String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));
const isNum = v => v!=null && v!=="" && !isNaN(v);

function pctSpan(v){ if(!isNum(v)) return '<span class="flat">—</span>';
  const n=Number(v), cls=n>0?"up":(n<0?"down":"flat"); return `<span class="${cls}">${n>0?"+":""}${n.toFixed(2)}%</span>`; }
function priceTxt(p){ return isNum(p)?Number(p).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}):"—"; }
function signCls(v){ return !isNum(v)?"flat":(Number(v)>0?"up":(Number(v)<0?"down":"flat")); }
function fx(v,d=1){ return isNum(v)?Number(v).toFixed(d):"—"; }
function dotFor(l){ const c=l==="red"?"red":(l==="amber"?"amber":"green"); return `<span class="dot ${c}"></span>`; }
function methodChip(m){ if(!m)return""; if(String(m).indexOf("20")>=0)return`<span class="chip m20">20分盤</span>`;
  if(String(m).indexOf("5")>=0)return`<span class="chip m5">5分盤</span>`; return`<span class="chip m5">${esc(m)}</span>`; }
function roundChip(r){ return (isNum(r)&&Number(r)>=2)?`<span class="chip r2">第${Number(r)}次</span>`:""; }

function mcellPair(la, va, clsa, lb, vb, clsb){
  return `<div class="mcell">
    <div class="mr"><span class="ml">${la}</span><span class="mv sm ${clsa||''}">${va}</span></div>
    <div class="mr"><span class="ml">${lb}</span><span class="mv sm ${clsb||''}">${vb}</span></div>
  </div>`;
}
function metricGrid(r, opt){
  opt=opt||{};
  const yx=r.yx, lf=r.lf, z5=r.z5, z10=r.z10;
  let rows = `
    ${mcellPair("位階", isNum(r.wj)?r.wj:"—", "", "月斜", isNum(yx)?fx(yx,1)+"%":"—", signCls(yx))}
    ${mcellPair("累幅", isNum(lf)?(lf>0?"+":"")+fx(lf,1)+"%":"—", signCls(lf), "剩天", isNum(r.st)?r.st:"—", "")}
    ${mcellPair("主5", isNum(z5)?fx(z5,1)+"%":"—", signCls(z5), "主10", isNum(z10)?fx(z10,1)+"%":"—", signCls(z10))}`;
  if(opt.watch){
    const pOk=isNum(lf)&&lf>=32, vOk=isNum(r.vmult)&&r.vmult>=5;
    rows += mcellPair("價格門檻≥32%", isNum(lf)?(lf>0?"+":"")+fx(lf,1)+"%":"—", pOk?"up":"amb",
                      "量價門檻≥5x", isNum(r.vmult)?fx(r.vmult,1)+"x":"—", vOk?"up":"amb");
  }
  return `<div class="mgrid">${rows}</div>`;
}
function cardHead(r, withLight){
  const chg=r.chg;
  return `<div class="top">
    <div class="lhs" onclick="goChart('${esc(r.sid)}')">
      <span>${withLight?dotFor(r.light):""}<span class="sid">${esc(r.sid)}</span><span class="nm">${esc(r.name||"")} ›</span>${r.mkt?`<span class="mkt">${esc(r.mkt)}</span>`:""}</span>
      ${r.ind?`<div class="cind">${esc(r.ind)}</div>`:""}
      <div class="period">${(r.start||r.end)?`處置 ${esc(r.start||"?")} ~ ${esc(r.end||"?")}`:""}</div>
    </div>
    <div class="px"><div class="p num">${priceTxt(r.close)}</div><div class="c">${pctSpan(chg)}</div></div>
  </div>`;
}
function progRow(r){
  if(!(r.day_n&&r.day_total)) return "";
  const pct=Math.max(0,Math.min(100,Math.round(r.day_n/r.day_total*100)));
  return `<div class="progline">
    ${methodChip(r.method)}${roundChip(r.round)}
    <div class="prog"><div class="progf" style="width:${pct}%"></div></div>
    <span class="pt">${r.day_n}/${r.day_total}${isNum(r.d2r)?`・剩${r.d2r}天`:""}</span>
  </div>`;
}
function renderList(elId, arr, opt){
  const el=$(elId); opt=opt||{};
  if(!arr||!arr.length){ el.innerHTML=`<div class="empty">${opt.empty||"目前沒有資料。"}</div>`; return; }
  el.innerHTML=arr.map(r=>`<div class="card">
    ${cardHead(r, opt.light)}
    ${metricGrid(r, opt)}
    ${opt.prog?progRow(r):""}
  </div>`).join("");
}

/* ---- 緊湊表格（仿處置神器）：凍結首欄、可左右滑動、點欄位標題排序 ---- */
const sortState = { notify:{key:null,asc:false}, watch:{key:null,asc:false}, confirmed:{key:null,asc:false}, ongoing:{key:null,asc:false}, release:{key:null,asc:false} };
const LISTDATA = { notify:[], watch:[], confirmed:[], ongoing:[], release:[] };
const LISTOPT = {
  notify:{prog:true, empty:"目前沒有達到通知標準的處置中個股。<br><span style=\"color:var(--dim)\">標準：月斜&gt;1% 且（累計跌幅破 -10/-20/-30%，或當日K棒觸及20MA月線）</span>"},
  watch:{light:true, empty:"目前沒有接近處置門檻的個股。"},
  confirmed:{empty:"目前沒有『已公告、明日起處置』的個股。<br><span style=\"color:var(--dim)\">交易所處置公告多在盤後傍晚發布；若今日剛公告，最快本次或次日盤後建置後出現。</span>"},
  ongoing:{prog:true, empty:"目前沒有剩餘 &gt;3 交易日的處置中個股。"},
  release:{prog:true, empty:"目前沒有即將出關（剩餘 ≤3 交易日）的處置中個股。"}
};
// 每欄堆疊 1~2 個(標籤,key)指標；watch 多一欄門檻、處置中/通知多一欄進度
function colSpec(name){
  // 明日確定：處置尚未開始 → 無「進度/剩天/累幅」，改看「近6漲」(近6日累積漲幅，被處置主因)
  if(name==="confirmed") return [
    [["股價","close"],["漲幅","chg"]],
    [["距峰高%","pk60"]],
    [["位階","wj"],["月斜","yx"]],
    [["近6漲","cum6"]],
    [["主5","z5"],["主10","z10"]],
  ];
  const c=[
    [["股價","close"],["漲幅","chg"]],
    [["距峰高%","pk60"]],
    [["位階","wj"],["月斜","yx"]],
    [["累幅","lf"],["剩天","st"]],
    [["主5","z5"],["主10","z10"]],
  ];
  if(name==="watch") c.push([["量倍","vmult"],["價門檻","lf"]]);
  if(name==="ongoing"||name==="notify"||name==="release") c.push([["進度","day_n"],["剩","d2r"]]);
  return c;
}
function fmtCell(key,v,r){
  if(key==="day_n"){ return (r.day_n&&r.day_total)?{t:r.day_n+"/"+r.day_total,c:""}:{t:"—",c:""}; }
  if(!isNum(v)) return {t:"—",c:"dim"};
  const n=Number(v);
  switch(key){
    case "close": return {t:priceTxt(n),c:""};
    case "chg":   return {t:(n>0?"+":"")+n.toFixed(2)+"%",c:signCls(n)};
    case "wj":    return {t:String(Math.round(n)),c:""};
    case "yx":    return {t:n.toFixed(1)+"%",c:signCls(n)};
    case "lf":    return {t:(n>0?"+":"")+n.toFixed(1)+"%", c:(r.light!=null&&n>=32)?"up":signCls(n)};
    case "st": case "d2r": return {t:String(Math.round(n)),c:""};
    case "z5": case "z10": return {t:n.toFixed(1)+"%",c:signCls(n)};
    case "cum6": return {t:(n>0?"+":"")+n.toFixed(1)+"%", c:signCls(n)};
    case "pk60": return {t:(n>0?"+":"")+n.toFixed(1)+"%", c:signCls(n)};
    case "vmult": return {t:n.toFixed(1)+"x", c:(n>=5?"up":"amb")};
  }
  return {t:String(n),c:""};
}
function sortRows(name){
  const s=sortState[name], arr=(LISTDATA[name]||[]).slice();
  if(s.key){ arr.sort((a,b)=>{ const av=a[s.key],bv=b[s.key],an=isNum(av),bn=isNum(bv);
    if(!an&&!bn)return 0; if(!an)return 1; if(!bn)return -1; return s.asc?(av-bv):(bv-av); }); }
  return arr;
}
let dispGroup=true;   // 依概念(退回產業)分群，預設開
/* 概念分群：概念(r.cpt 主概念)優先，退回產業(r.ind)，再退回未分類；概念群在前、產業次之、未分類最後 */
function dispGroupBy(rows){
  const gm={};
  rows.forEach(r=>{
    const cs=(r.cpt&&r.cpt.length)?r.cpt:null;
    let name, isC;
    if(cs){ name=cs[0]; isC=true; } else { name=r.ind||"未分類"; isC=false; }
    (gm[name]||(gm[name]={name,isConcept:isC,rows:[]})).rows.push(r);
  });
  const arr=Object.keys(gm).map(k=>gm[k]);
  arr.sort((a,b)=>{ const ap=a.name==="未分類"?2:(a.isConcept?0:1), bp=b.name==="未分類"?2:(b.isConcept?0:1);
    if(ap!==bp)return ap-bp; if(b.rows.length!==a.rows.length)return b.rows.length-a.rows.length; return a.name.localeCompare(b.name); });
  return arr;
}
function dispGroupHdr(g,cols){ return `<tr class="grouphdr ${g.isConcept?'gc':'gi'}"><td colspan="${cols}"><span class="ghlbl"><span class="gchip">${g.isConcept?'概念':'產業'}</span>${esc(g.name)}<span class="gcount">${g.rows.length}檔</span></span></td></tr>`; }
function dispRowHtml(r,cols){
    const sc=isNum(r.chg)?(r.chg>0?"up":(r.chg<0?"down":"")):"";
    let tds="";
    cols.forEach(col=>{ tds+="<td>"+col.map(([lab,key])=>{ const f=fmtCell(key,r[key],r); return `<span class="cv ${f.c}">${f.t}</span>`; }).join("<br>")+"</td>"; });
    const per=(r.start||r.end)?`${esc((r.start||"").slice(5))}~${esc((r.end||"").slice(5))}`:"";
    return `<tr class="side-${sc}" onclick="goChart('${esc(r.sid)}')">
      <td class="frz nmcell">
        <div class="nm">${r.light?dotFor(r.light):""}${esc(r.name||"")}${methodChip(r.method)}${roundChip(r.round)}</div>
        <div class="sub">${esc(r.sid)}${r.mkt?" "+esc(r.mkt):""}</div>
        ${r.ind?`<div class="cind">${esc(r.ind)}</div>`:""}
        ${per?`<div class="per">${per}</div>`:""}
        ${(r.reasons&&r.reasons.length)?`<div class="nrz">${r.reasons.map(x=>`<span class="ntag">${esc(x)}</span>`).join("")}</div>`:""}
      </td>${tds}</tr>`;
}
function renderTbl(name){
  const el=$("list-"+name); if(!el) return;
  const data=LISTDATA[name]||[];
  if(!data.length){ el.innerHTML=`<div class="empty">${(LISTOPT[name]||{}).empty||"目前沒有資料。"}</div>`; return; }
  const s=sortState[name], cols=colSpec(name), rows=sortRows(name);
  const arrow=(k)=> s.key===k?(s.asc?"▲":"▼"):"";
  let thead=`<th class="frz">名稱<br><span class="sub">代號 / 處置期</span></th>`;
  cols.forEach(col=>{ thead+="<th>"+col.map(([lab,key])=>
    `<span class="sortlbl${s.key===key?' on':''}" data-n="${name}" data-k="${key}">${lab}<i>${arrow(key)}</i></span>`).join("<br>")+"</th>"; });
  const ncol=cols.length+1;
  let tb="";
  if(dispGroup){ dispGroupBy(rows).forEach(g=>{ tb+=dispGroupHdr(g,ncol)+g.rows.map(r=>dispRowHtml(r,cols)).join(""); }); }
  else { tb=rows.map(r=>dispRowHtml(r,cols)).join(""); }
  el.innerHTML=`<div class="dtbl-wrap"><table class="dtbl"><thead><tr>${thead}</tr></thead><tbody>${tb}</tbody></table></div>`;
  el.querySelectorAll(".sortlbl").forEach(b=>b.addEventListener("click",ev=>{ ev.stopPropagation();
    const n=b.dataset.n,k=b.dataset.k,st=sortState[n]; if(st.key===k)st.asc=!st.asc; else{st.key=k;st.asc=false;} renderTbl(n); }));
}

const EXPL = `
<details class="expl"><summary>指標說明（點開）</summary><div class="expbody">
這些指標協助快速判讀。<b>台股紅漲綠跌</b>。表格可<b>左右滑動</b>看更多指標、<b>點欄位標題排序</b>（再點同一欄切換升/降冪）、<b>點一列</b>看該股 K 線。
<div class="g">
<span class="k">處置 起~迄</span><span>該股分盤處置的起始與結束日。</span>
<span class="k">產業</span><span>股名下方小字＝產業鏈分類（主要個股為上中下游＋子類，其餘為大分類）。</span>
<span class="k">距峰高%</span><span>目前收盤價 相對「近 60 日內『最高收盤價』那天的<b>盤中最高價</b>」的距離%。公式＝(今收 ÷ 該峰日最高價 − 1)×100，多為負值(收盤低於該峰高)；<b>越負代表距近期高點回檔越深</b>，處置中被錯殺時常是低接觀察點。</span>
<span class="k">位階</span><span>20日布林通道整數級距：<b>+10</b>＝上軌(基期偏高、較適放空)、<b>0</b>＝月線(中線)、<b>-10</b>＝下軌(基期偏低、較適做多)。公式＝round((收盤−MA20)÷(2×20日標準差)×10)，夾在 ±10。</span>
<span class="k">月斜</span><span>月線(20MA)1日斜率%＝(MA20今−MA20昨)÷MA20昨×100。<b>&gt;1%＝強勢、&gt;3%＝妖股</b>。</span>
<span class="k">累幅</span><span>處置中＝(今收−處置前一日收)÷處置前一日收×100；可能進入處置＝近6日累積漲幅%。</span>
<span class="k">剩天</span><span>處置中＝距出關交易日數；可能進入處置＝最快可能進入處置的天數。</span>
<span class="k">主5／主10</span><span>近5/10日集中度%＝(前15大買方券商買超總和 − 前15大賣方券商賣超總和)÷該區間成交量×100。正(紅)＝主力買超集中；負(綠)＝主力派發。</span>
<span class="k">價格門檻</span><span>注意股漲幅型(第1款)：近6日累積漲幅 ≥ <b>32%</b>。值達標轉紅。</span>
<span class="k">量價門檻</span><span>注意股量能型：當日量 ≥ 近60日均量 × <b>5倍</b>。值達標轉紅。</span>
</div>
<div style="margin-top:9px; color:var(--dim)">點名稱可跳到該股 K 線；K 線下方副圖可切「主力買賣超」看逐日紅綠柱＋累計線。「可能進入處置」為本站用收盤價/量自算的注意/處置門檻估計，實際以證交所、櫃買最新公告為準。</div>
</div></details>`;
const NOTIFY_EXPL = `
<details class="expl"><summary>通知規則（點開）</summary><div class="expbody">
每次資料更新都<b>重新掃描一次</b>處置中個股，符合下列標準者進通知：
<div class="g">
<span class="k">前提</span><span>該股<b>月線斜率(月斜) &gt; 1%</b>（月線仍偏多）。</span>
<span class="k">觸發</span><span>下列任一：累計跌幅破 <b>-10% / -20% / -30%</b>，或<b>當日K棒觸及20MA月線價格</b>（今日最低 ≤ MA20 ≤ 今日最高）。</span>
</div>
<div style="margin-top:9px; color:var(--dim)">用意：強勢(月線向上)的處置中個股拉回到月線、或跌幅到關卡，常是「浪子回頭」低接觀察點。跌幅越深排越前。非投資建議。</div>
</div></details>`;

function goChart(sid){ location.href = "index.html?stk=" + encodeURIComponent(sid); }

function switchTab(p){
  document.querySelectorAll(".czt").forEach(b=>b.classList.toggle("on", b.dataset.p===p));
  ["ov","notify","watch","confirmed","ongoing","release","teach","rule"].forEach(x=>{
    const n=$("p-"+x); if(n) n.classList.toggle("hidden", x!==p);
  });
  try{ window.scrollTo({top:0,behavior:"smooth"}); }catch(e){ window.scrollTo(0,0); }
}
document.querySelectorAll(".czt").forEach(b=>b.addEventListener("click",()=>switchTab(b.dataset.p)));

async function boot(){
  let d=null;
  try{ const r=await fetch("data/chuzhi.json?v="+BUILD_V,{cache:"default"}); if(r.ok) d=await r.json(); }catch(e){}
  ["watch","confirmed","ongoing","release"].forEach(k=>{ const e=$("expl-"+k); if(e) e.innerHTML=EXPL; });
  const en=$("expl-notify"); if(en) en.innerHTML=NOTIFY_EXPL;
  if(!d){
    $("today").textContent="資料尚未產生";
    ["notify","watch","confirmed","ongoing","release"].forEach(k=>{ const el=$("list-"+k); if(el) el.innerHTML=`<div class="empty">尚未取得處置資料。<br>請先在 GitHub Actions 跑一次工作流程產生 data/chuzhi.json。</div>`; });
    return;
  }
  $("today").textContent=d.today||"—";
  $("gentime").textContent=d.gentime||"—";
  const c=d.counts||{};
  $("cnt-n").textContent=c.notify!=null?c.notify:((d.notify||[]).length);
  $("cnt-w").textContent=c.watch!=null?c.watch:((d.watch||[]).length);
  const _cc=$("cnt-c"); if(_cc) _cc.textContent=c.confirmed!=null?c.confirmed:((d.confirmed||[]).length);
  $("cnt-o").textContent=c.ongoing!=null?c.ongoing:((d.ongoing||[]).length);
  const _rs=$("cnt-rs"); if(_rs) _rs.textContent=c.release_soon!=null?c.release_soon:((d.release_soon||[]).length);
  LISTDATA.notify=d.notify||[]; LISTDATA.watch=d.watch||[]; LISTDATA.confirmed=d.confirmed||[]; LISTDATA.ongoing=d.ongoing||[]; LISTDATA.release=d.release_soon||[];
  ["notify","watch","confirmed","ongoing","release"].forEach(renderTbl);
  if(d.diag && (d.diag.notes||[]).length){
    const box=$("diagbox"); box.style.display="block";
    box.innerHTML="<b>資料診斷</b>："+(d.diag.notes||[]).map(esc).join("；");
  }
}
const _dgt=document.getElementById("dispGtog");
if(_dgt) _dgt.addEventListener("click",e=>{ dispGroup=!dispGroup; e.currentTarget.classList.toggle("on",dispGroup); ["notify","watch","confirmed","ongoing"].forEach(n=>renderTbl(n)); });
boot();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
