# -*- coding: utf-8 -*-
r"""
處置股專區資料產生器（tw_disposition.py）v2
================================================================
產生 site/data/chuzhi.json 與 site/chuzhi.html，並把每檔「主力買賣超日序列」
回寫進 site/data/{代號}.json（給 K 線副圖切換用）。
與 build_site.py / tw_volume_breakout_screener_v2.py 共用 twstock.db 與 FinMind 串接。

四狀態：watch(漲幅型估計) / confirmed(明日確定) / ongoing(處置中) / released(剛出關)
每檔指標（仿處置神器）：連次 連量 位階 月斜 累幅 剩天 主5 主10，主力買賣超日序列。

資料來源：TaiwanStockDispositionSecuritiesPeriod、TaiwanStockTradingDailyReport(分點,Sponsor)、twstock.db
用法：
  python tw_disposition.py             # 正常
  python tw_disposition.py --demo      # 離線示範
  python tw_disposition.py --no-chips  # 跳過分點(省流量)
"""
import os, sys, json, time, sqlite3, datetime, argparse
from statistics import pstdev
import pandas as pd
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
MF_HISTORY_DAYS = 40          # 主力序列最多看回幾個交易日
MF_BACKFILL_CAP = 10          # 每檔每次最多補抓幾日（首次回補上限；之後每日約+1）
RELEASED_WINDOW_TD = 5
WATCH_CUM6_MIN = 25.0
K1_THRESHOLD = 32.0

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

def compute_price_metrics(seq, idx6=0.0, disp_start=None):
    """seq: [(date,high,low,close,volume) 由舊到新]。回傳指標 dict。"""
    closes = [r[3] for r in seq if r[3] is not None]
    highs = [r[1] for r in seq if r[1] is not None]
    lows = [r[2] for r in seq if r[2] is not None]
    vols = [r[4] for r in seq if r[4] is not None]
    dates = [r[0] for r in seq if r[3] is not None]
    out = {"chg": None, "cum6": None, "lc": None, "ll": None, "wj": None, "yx": None, "lf": None}
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
    # 月斜：月線(MA20) 1日斜率%（小哥定義：>1%強勢、>3%妖股）
    ma20 = _ma(closes, 20); ma20_1 = _ma(closes[:-1], 20) if len(closes) >= 21 else None
    if ma20 and ma20_1: out["yx"] = round((ma20/ma20_1-1)*100, 2)
    # 位階：小哥用「布林通道」定義，+10基期高/-10基期低 → (收盤-MA20)/(2*STD20)*10
    if len(closes) >= 20 and ma20:
        sd = pstdev(closes[-20:])
        if sd > 0:
            out["wj"] = round(max(-15.0, min(15.0, (last-ma20)/(2*sd)*10)), 1)
    # 累幅
    if disp_start:
        sc = None
        for d,_,_,c,_ in seq:
            if d >= disp_start and c is not None: sc = c; break
        out["lf"] = round((last/sc-1)*100, 1) if sc else (out["cum6"])
    else:
        out["lf"] = out["cum6"]
    return out

def watch_estimate_days(cum6, lc):
    if cum6 is None: return None
    if cum6 >= K1_THRESHOLD: return 1 if lc and lc >= 2 else 2
    return None

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

def patch_stock_mf(out_dir, sid, mf_series):
    """把主力買賣超日序列寫進 site/data/{sid}.json 的 mfs/mf。"""
    p = os.path.join(out_dir, "data", f"{sid}.json")
    if not os.path.exists(p) or not mf_series: return False
    try:
        with open(p, encoding="utf-8") as f: o = json.load(f)
    except Exception: return False
    d = o.get("d", [])
    if not d: return False
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

# ===== 組裝/輸出 =====
def build_payload(today, next_td, watch, confirmed, ongoing, released, diag):
    return {"gentime": now_taipei(), "today": today, "next_td": next_td,
            "counts": {"watch": len(watch), "confirmed": len(confirmed),
                       "ongoing": len(ongoing), "released": len(released)},
            "diag": diag, "watch": watch, "confirmed": confirmed,
            "ongoing": ongoing, "released": released}

def write_outputs(out_dir, payload):
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
    with open(os.path.join(out_dir, "data", "chuzhi.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    with open(os.path.join(out_dir, "chuzhi.html"), "w", encoding="utf-8") as f:
        f.write(CHUZHI_HTML)
    c = payload["counts"]
    print(f"已寫出 {out_dir}/chuzhi.html 與 data/chuzhi.json "
          f"（watch {c['watch']}・確定 {c['confirmed']}・處置中 {c['ongoing']}・出關 {c['released']}）")

# ===== 示範資料 =====
def make_demo():
    today = "2026-06-27"
    diag = {"notes": ["[示範模式] 合成資料，非真實行情"], "disp_cols": []}
    def row(sid,name,mkt,close,chg,**kw):
        d = {"sid":sid,"name":name,"mkt":mkt,"close":close,"chg":chg}; d.update(kw); return d
    watch = [
        row("4129","聯合","上市",58.9,9.92,light="red",lc=2,ll=243,wj=8.5,yx=2.1,lf=33.8,st=1,z5=11.2,z10=8.4),
        row("3083","網龍","上櫃",102.0,3.55,light="amber",lc=0,ll=168,wj=6.2,yx=1.4,lf=26.1,st=2,z5=-3.4,z10=-1.1),
    ]
    confirmed = [
        row("2618","長榮航","上市",48.6,9.95,round=1,method="5分盤",start="2026-06-30",end="2026-07-11",
            lc=3,ll=251,wj=5.1,yx=1.6,lf=-14.0,st=10,z5=-6.4,z10=-5.2),
    ]
    ongoing = [
        row("2484","希華","上市",42.55,2.53,round=1,method="5分盤",start="2026-06-23",end="2026-07-04",
            day_n=3,day_total=10,release="2026-07-04",d2r=1,lc=3,ll=251,wj=0.3,yx=1.6,lf=-14.0,st=1,z5=-6.4,z10=-5.2),
        row("3339","泰谷","上市",59.8,-0.33,round=1,method="5分盤",start="2026-06-23",end="2026-07-07",
            day_n=3,day_total=11,release="2026-07-07",d2r=2,lc=3,ll=176,wj=1.9,yx=1.9,lf=-13.0,st=2,z5=7.1,z10=1.3),
        row("8289","泰藝","上市",49.35,5.45,round=2,method="20分盤",start="2026-06-23",end="2026-07-09",
            day_n=11,day_total=12,release="2026-07-09",d2r=1,lc=11,ll=168,wj=0.4,yx=1.5,lf=-13.0,st=1,z5=-6.2,z10=-7.2),
    ]
    released = [
        row("6442","光聖","上市",2060,-1.20,end="2026-06-26",since=1,lc=1,ll=42,wj=-2.0,yx=0.1,lf=-12.0,st=0,z5=2.4,z10=-7.7),
    ]
    return build_payload(today, next_trading_day(today), watch, confirmed, ongoing, released, diag)

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

    # 處置名單
    disp_recs = []
    try:
        ds = (datetime.date.fromisoformat(today) - datetime.timedelta(days=45)).isoformat()
        df = finmind_get("TaiwanStockDispositionSecuritiesPeriod", FINMIND_TOKEN, start_date=ds)
        diag["notes"].append(f"處置名單 {0 if df is None else len(df)} 筆")
        disp_recs = parse_disposition(df, diag)
    except Exception as e:
        diag["notes"].append(f"處置名單抓取失敗：{e}")
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

    # watch（漲幅型估計）
    watch = []
    for sid, seq in win.items():
        if sid in disp_sids: continue
        if len(seq) < 7: continue
        m = compute_price_metrics(seq, idx6)
        if m["cum6"] is None or m["cum6"] < WATCH_CUM6_MIN: continue
        light = "red" if m["cum6"] >= K1_THRESHOLD else "amber"
        watch.append({"sid": sid, "name": names.get(sid,""), "mkt": mkts.get(sid,""),
                      "close": round(seq[-1][3],2), "chg": m["chg"], "light": light,
                      "lc": m["lc"], "ll": m["ll"], "wj": m["wj"], "yx": m["yx"],
                      "lf": m["cum6"], "st": watch_estimate_days(m["cum6"], m["lc"]),
                      "z5": None, "z10": None})
    watch.sort(key=lambda x: (0 if x["light"]=="red" else 1, -(x["lf"] or 0)))

    # 處置三類補價格指標
    for lst, with_start in ((ongoing, True), (confirmed, True), (released, True)):
        for r in lst:
            seq = win.get(r["sid"], [])
            if seq:
                m = compute_price_metrics(seq, idx6, disp_start=r.get("start"))
                r["close"] = round(seq[-1][3], 2)
                r.update({"chg": m["chg"], "lc": m["lc"], "ll": m["ll"], "wj": m["wj"],
                          "yx": m["yx"], "lf": m["lf"]})
            r["name"] = names.get(r["sid"], r.get("name","")); r["mkt"] = mkts.get(r["sid"], "")
            r.setdefault("z5", None); r.setdefault("z10", None)
    # 剩天
    for r in ongoing: r["st"] = r.get("d2r")
    for r in confirmed: r["st"] = None
    for r in released: r["st"] = 0

    # 分點：用快取表累積每日主力買賣超，只補抓「還沒存過」的近幾天（單日抓，FinMind 分點不支援區間）
    if not args.no_chips and FINMIND_TOKEN:
        ensure_mf_table(con)
        targets = list(dict.fromkeys([r["sid"] for r in ongoing] + [r["sid"] for r in confirmed]
                                     + [r["sid"] for r in released] + [r["sid"] for r in watch]))[:CHIP_MAX_STOCKS]
        mf_dates = trading_dates(con, MF_HISTORY_DAYS)
        idx_for = {r["sid"]: r for lst in (ongoing,confirmed,released,watch) for r in lst}
        fetched = 0; patched = 0; dbg_done = False
        for sid in targets:
            have = cached_mf_dates(con, sid)
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
                fetched += 1
                time.sleep(CHIP_SLEEP)
            con.commit()
            ser = load_mf_series(con, sid, mf_dates)
            if patch_stock_mf(args.out, sid, ser): patched += 1
            seq = win.get(sid, [])
            voln = {dd: (v/1000.0) for dd,_,_,_,v in seq if v}
            r = idx_for.get(sid)
            if r is not None and ser:
                r["z5"] = window_cc(ser, voln, mf_dates[-5:])
                r["z10"] = window_cc(ser, voln, mf_dates[-10:])
        diag["notes"].append(f"分點：本次抓 {fetched} 日，主力序列回寫 {patched}/{len(targets)} 檔（快取累積中）")
    con.close()
    write_outputs(args.out, build_payload(today, next_td, watch, confirmed, ongoing, released, diag))

CHUZHI_HTML = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0a0f1a">
<title>處置股專區 ・ 台股看板</title>
<link rel="manifest" href="manifest.json">
<style>
  :root{
    --bg:#0a0f1a; --card:#111827; --card2:#161f33; --border:#1f2a3d;
    --text:#e6edf6; --muted:#93a3b8; --dim:#5e6f86;
    --amber:#f5a524; --amber-s:rgba(245,165,36,.14);
    --up:#ff4d4f; --down:#22c55e;
    --blue:#4d9fff; --blue-s:rgba(77,159,255,.12);
    --purple:#b794ff; --purple-s:rgba(183,148,255,.12);
    --red-s:rgba(255,77,79,.13); --grn-s:rgba(34,197,94,.13);
  }
  *{box-sizing:border-box;}
  body{margin:0; background:var(--bg); color:var(--text);
    font-family:'Inter','Noto Sans TC','PingFang TC','Microsoft JhengHei',system-ui,sans-serif;
    -webkit-font-smoothing:antialiased; padding:16px 12px 40px; padding-top:calc(16px + env(safe-area-inset-top));}
  .num{font-variant-numeric:tabular-nums;}
  .wrap{max-width:1180px; margin:0 auto;}
  a{color:var(--blue); text-decoration:none;}
  header h1{font-size:19px; font-weight:800; margin:0;}
  .sub{font-size:12px; color:var(--muted); margin-top:5px; line-height:1.5;}
  .hidden{display:none !important;}

  .cztabs{display:flex; gap:6px; margin:14px 0; background:var(--card); padding:5px; border-radius:11px;
    border:1px solid var(--border); overflow-x:auto; -webkit-overflow-scrolling:touch; scrollbar-width:none;}
  .cztabs::-webkit-scrollbar{display:none;}
  .czt{flex:0 0 auto; background:transparent; color:var(--muted); border:none; border-radius:8px;
    padding:9px 13px; font-size:13.5px; font-weight:700; cursor:pointer; white-space:nowrap;}
  .czt.on{background:var(--amber-s); color:var(--amber);}

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

  .card{background:var(--card); border:1px solid var(--border); border-radius:13px; padding:12px 14px; margin-bottom:9px;}
  .card .top{display:flex; align-items:flex-start; gap:9px;}
  .card .lhs{flex:1; min-width:0; cursor:pointer;}
  .card .sid{font-size:16px; font-weight:800; color:var(--text); font-variant-numeric:tabular-nums;}
  .card .nm{font-size:14px; color:var(--blue); margin-left:7px;}
  .card .lhs:active .nm{opacity:.6;}
  .card .period{font-size:11px; color:var(--dim); margin-top:3px; font-variant-numeric:tabular-nums;}
  .card .mkt{font-size:10.5px; color:var(--dim); border:1px solid var(--border); border-radius:5px; padding:1px 6px; margin-left:6px;}
  .card .px{text-align:right; flex:none;}
  .card .px .p{font-size:16px; font-weight:800; font-variant-numeric:tabular-nums;}
  .card .px .c{font-size:12.5px; font-weight:700; font-variant-numeric:tabular-nums;}

  .mgrid{display:grid; grid-template-columns:repeat(4,1fr); gap:7px 6px; margin-top:11px;}
  .mcell{background:var(--card2); border-radius:8px; padding:7px 8px; line-height:1.32;}
  .mcell .mr{display:flex; justify-content:space-between; align-items:baseline; gap:4px;}
  .mcell .ml{font-size:10px; color:var(--dim); font-weight:600;}
  .mcell .mv{font-size:13.5px; font-weight:800; font-variant-numeric:tabular-nums;}
  .mcell .mv.sm{font-size:12px;}

  .up{color:var(--up);} .down{color:var(--down);} .flat{color:var(--muted);} .amb{color:var(--amber);}
  .dot{display:inline-block; width:9px; height:9px; border-radius:99px; margin-right:2px; vertical-align:middle;}
  .dot.red{background:var(--up); box-shadow:0 0 7px rgba(255,77,79,.7);}
  .dot.amber{background:var(--amber); box-shadow:0 0 7px rgba(245,165,36,.7);}
  .dot.green{background:var(--down);}

  .prog{height:6px; background:#0e1626; border-radius:5px; overflow:hidden; flex:1;}
  .progf{height:100%; background:linear-gradient(90deg,#4d9fff,#27c4dc); border-radius:5px;}
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

  .doc{background:var(--card); border:1px solid var(--border); border-radius:13px; padding:17px 18px; line-height:1.72; font-size:14px;}
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
    <div class="sub">資料日 <span id="today" class="num">—</span> ・ 更新 <span id="gentime" class="num">—</span>（台北）　<a href="index.html">← 回主看板</a></div>
  </header>

  <div class="cztabs" id="cztabs">
    <button class="czt on" data-p="ov">總覽</button>
    <button class="czt" data-p="watch">即將進處置</button>
    <button class="czt" data-p="confirm">明日確定</button>
    <button class="czt" data-p="ongoing">處置中</button>
    <button class="czt" data-p="released">剛出關</button>
    <button class="czt" data-p="teach">實戰教學</button>
    <button class="czt" data-p="rule">規則說明</button>
  </div>

  <div class="pane" id="p-ov">
    <div class="stats">
      <div class="stat w"><div class="n num" id="cnt-w">—</div><div class="l">即將進處置</div></div>
      <div class="stat c"><div class="n num" id="cnt-c">—</div><div class="l">明日確定</div></div>
      <div class="stat o"><div class="n num" id="cnt-o">—</div><div class="l">處置中</div></div>
      <div class="stat r"><div class="n num" id="cnt-r">—</div><div class="l">剛出關</div></div>
    </div>
    <div class="note">
      <b>燈號</b>：<span class="dot red"></span>紅＝今日已觸發漲幅型（第1款）　<span class="dot amber"></span>黃＝接近門檻　<span class="dot green"></span>綠＝安全。<br>
      點任何個股的<b style="color:var(--blue)">名稱</b>可跳到該股 K 線圖（K 線下方副圖可切換 MACD／主力買賣超）。各指標意義見每個清單最下方「指標說明」。分點資料：FinMind Sponsor（T+1 盤後）。
    </div>
    <div class="note" id="diagbox" style="display:none"></div>
  </div>

  <div class="pane hidden" id="p-watch">
    <div class="sech">📈 即將/可能進處置 <span class="pill">漲幅型估計・盤後自算</span></div>
    <div id="list-watch"></div>
    <div id="expl-watch"></div>
  </div>

  <div class="pane hidden" id="p-confirm">
    <div class="sech">🔒 下一交易日確定進入處置 <span class="pill">FinMind 處置公告</span></div>
    <div id="list-confirm"></div>
    <div id="expl-confirm"></div>
  </div>

  <div class="pane hidden" id="p-ongoing">
    <div class="sech">⛓️ 處置中（坐牢） <span class="pill">分盤交易期</span></div>
    <div id="list-ongoing"></div>
    <div id="expl-ongoing"></div>
  </div>

  <div class="pane hidden" id="p-released">
    <div class="sech">🕊️ 剛脫離處置 <span class="pill">近5個交易日內出關</span></div>
    <div id="list-released"></div>
    <div id="expl-released"></div>
  </div>

  <div class="pane hidden" id="p-teach">
    <div class="doc">
      <h3>一句話：處置股在玩什麼？</h3>
      <p class="lead">股票短期漲太兇（或量／當沖太誇張）會被交易所「關起來」分盤交易、限制當沖。流動性被抽乾後籌碼鎖死，股價容易<b>暴漲暴跌</b>。我們不賭它被關，而是抓「進處置前、處置中、出關」三個時點的大波動。</p>
      <h3>五種狀態，你各該做什麼</h3>
      <table>
        <tr><th>狀態</th><th>白話</th><th>怎麼做</th></tr>
        <tr><td class="k">即將進處置</td><td>漲幅接近門檻、聽牌中</td><td>賭公告前最後一漲：須月線向上、主力沒跑；公告後可能跳水，控好部位</td></tr>
        <tr><td class="k">明日確定</td><td>明天起被關</td><td>波段客可尾盤卡位「越關越大尾」標的；當沖客準備出關日反向操作</td></tr>
        <tr><td class="k">處置中</td><td>分盤坐牢</td><td>低接「拉回月線且主力沒跑」；嚴禁現股當沖；跌破月線或主力撤退就走</td></tr>
        <tr><td class="k">剛出關</td><td>恢復正常交易</td><td>首日多開高走低→偏空；要續抱等「帶量過前高」再進</td></tr>
      </table>
      <h3>兩個核心買點</h3>
      <div class="step"><div class="no">1</div><div class="tx"><b>浪子回頭</b>：強勢股拉回月線（20日線）止跌、量縮、主力沒走（月斜為正、集中度為正、距月線±3%內、量縮到平常3成以下）。</div></div>
      <div class="step"><div class="no">2</div><div class="tx"><b>深蹲蓄力</b>：處置期被一根大單殺到近跌停（單日跌約9%↑），但盤後主力沒在倒貨（集中度沒轉負）＝恐慌錯殺，易反彈。</div></div>
      <h3>鐵則</h3>
      <div class="warn">① 收盤跌破月線超過3%、或主力集中度連3日轉負 → 隔天無條件出。② 不玩出關當天（開高走低機率高）。③ 當沖税費＋分盤滑價常吃光價差。④ 處置期間禁現股當沖。⑤ 漲停家數＞20 或處置股暴增＝末段訊號，降部位。</div>
      <p class="discl">本頁為交易紀律整理，非投資建議。門檻數字為對公開教學的量化重構，需依你自己的回測校準。</p>
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

<script>
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
function metricGrid(r){
  const yx=r.yx, lf=r.lf, z5=r.z5, z10=r.z10;
  return `<div class="mgrid">
    ${mcellPair("連次", isNum(r.lc)?r.lc:"—", r.light==="red"?"up":"", "連量", isNum(r.ll)?Math.round(r.ll):"—", "amb")}
    ${mcellPair("位階", isNum(r.wj)?r.wj:"—", "", "月斜", isNum(yx)?fx(yx,1):"—", signCls(yx))}
    ${mcellPair("累幅", isNum(lf)?(lf>0?"+":"")+fx(lf,1)+"%":"—", signCls(lf), "剩天", isNum(r.st)?r.st:"—", "")}
    ${mcellPair("主5", isNum(z5)?fx(z5,1):"—", signCls(z5), "主10", isNum(z10)?fx(z10,1):"—", signCls(z10))}
  </div>`;
}
function cardHead(r, withLight){
  const chg=r.chg;
  return `<div class="top">
    <div class="lhs" onclick="goChart('${esc(r.sid)}')">
      <span>${withLight?dotFor(r.light):""}<span class="sid">${esc(r.sid)}</span><span class="nm">${esc(r.name||"")} ›</span>${r.mkt?`<span class="mkt">${esc(r.mkt)}</span>`:""}</span>
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
    ${metricGrid(r)}
    ${opt.prog?progRow(r):""}
  </div>`).join("");
}

const EXPL = `
<details class="expl"><summary>指標說明（點開）</summary><div class="expbody">
這些指標仿照「處置神器」的呈現方式，協助快速判讀。<b>台股紅漲綠跌</b>。
<div class="g">
<span class="k">處置 起~迄</span><span>該股分盤處置的起始與結束日。</span>
<span class="k">股價／漲幅</span><span>當日收盤價與當日漲跌幅。</span>
<span class="k">連次</span><span>近期連續達「漲幅型注意（第1款）」的天數；越大代表越強勢、越接近升級處置。</span>
<span class="k">連量</span><span>量比＝今日量 ÷ 近20日均量 ×100。&gt;200 為明顯爆量。</span>
<span class="k">位階</span><span>小哥用「布林通道」定義：<b>+10</b>＝股價基期偏高(近上軌、較適放空)、<b>-10</b>＝基期偏低(近下軌、較適做多)、0＝在月線。公式＝(收盤−MA20)÷(2×20日標準差)×10。</span>
<span class="k">月斜</span><span>月線(20日線)1日斜率%。小哥定義：<b>&gt;1%＝強勢股、&gt;3%＝妖股</b>。公式＝(MA20今−MA20昨)÷MA20昨×100。</span>
<span class="k">累幅</span><span>處置中／出關股＝自處置起算的累積漲跌%；即將進處置股＝近6日累積漲幅%。</span>
<span class="k">剩天</span><span>距出關還剩幾個交易日（即將進處置股顯示距處置估計天數）。</span>
<span class="k">主5／主10</span><span>近5日／近10日「籌碼集中度%」＝(前15買超分點 − 前15賣超分點)÷區間總量×100。正(紅)＝籌碼集中在主力；負(綠)＝主力派發。</span>
</div>
<div style="margin-top:9px; color:var(--dim)">點名稱可跳到該股 K 線；K 線下方副圖可切「主力買賣超」看逐日紅綠柱＋累計線。「即將進處置」為本站用收盤價自算的漲幅型估計，實際以證交所、櫃買公告為準。</div>
</div></details>`;

function goChart(sid){ location.href = "index.html?stk=" + encodeURIComponent(sid); }

function switchTab(p){
  document.querySelectorAll(".czt").forEach(b=>b.classList.toggle("on", b.dataset.p===p));
  ["ov","watch","confirm","ongoing","released","teach","rule"].forEach(x=>{
    const n=$("p-"+x); if(n) n.classList.toggle("hidden", x!==p);
  });
  try{ window.scrollTo({top:0,behavior:"smooth"}); }catch(e){ window.scrollTo(0,0); }
}
document.querySelectorAll(".czt").forEach(b=>b.addEventListener("click",()=>switchTab(b.dataset.p)));

async function boot(){
  let d=null;
  try{ const r=await fetch("data/chuzhi.json",{cache:"default"}); if(r.ok) d=await r.json(); }catch(e){}
  ["watch","confirm","ongoing","released"].forEach(k=>{ const e=$("expl-"+k); if(e) e.innerHTML=EXPL; });
  if(!d){
    $("today").textContent="資料尚未產生";
    ["watch","confirm","ongoing","released"].forEach(k=>{ const el=$("list-"+k); if(el) el.innerHTML=`<div class="empty">尚未取得處置資料。<br>請先在 GitHub Actions 跑一次工作流程產生 data/chuzhi.json。</div>`; });
    return;
  }
  $("today").textContent=d.today||"—";
  $("gentime").textContent=d.gentime||"—";
  const c=d.counts||{};
  $("cnt-w").textContent=c.watch!=null?c.watch:((d.watch||[]).length);
  $("cnt-c").textContent=c.confirmed!=null?c.confirmed:((d.confirmed||[]).length);
  $("cnt-o").textContent=c.ongoing!=null?c.ongoing:((d.ongoing||[]).length);
  $("cnt-r").textContent=c.released!=null?c.released:((d.released||[]).length);
  renderList("list-watch", d.watch||[], {light:true, empty:"目前沒有接近處置門檻的個股。"});
  renderList("list-confirm", d.confirmed||[], {empty:"下一交易日沒有新進處置的個股。"});
  renderList("list-ongoing", d.ongoing||[], {prog:true, empty:"目前沒有處置中的個股。"});
  renderList("list-released", d.released||[], {empty:"近5個交易日內沒有出關的個股。"});
  if(d.diag && (d.diag.notes||[]).length){
    const box=$("diagbox"); box.style.display="block";
    box.innerHTML="<b>資料診斷</b>："+(d.diag.notes||[]).map(esc).join("；");
  }
}
boot();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
