# -*- coding: utf-8 -*-
r"""
處置股專區資料產生器（tw_disposition.py）
================================================================
產生 site/data/chuzhi.json 與 site/chuzhi.html，供「處置股專區」頁面使用。
與既有 build_site.py / tw_volume_breakout_screener_v2.py 共用 twstock.db 與 FinMind 串接。

四種狀態：
  ● 即將進處置（watch）   ：用 twstock.db 收盤價自算「漲幅型(第1款)」估計（免費，盤後）。
  ● 明日確定（confirmed） ：FinMind 處置名單中，起日 = 下一交易日者。
  ● 處置中（ongoing）     ：今日 ∈ [起日, 迄日]。
  ● 剛出關（released）    ：迄日在最近 N 個交易日內。
另對 ongoing/confirmed/watch 子集，用 FinMind 分點計算「主力買賣超 / 籌碼集中度CC15」（需 Sponsor）。

資料來源與權限：
  - TaiwanStockDispositionSecuritiesPeriod（處置名單）→ FinMind Backer/Sponsor
  - TaiwanStockTradingDailyReport（分點）            → FinMind Sponsor
  - TaiwanSecuritiesTraderInfo（券商代碼，可選）     → 免費
  - twstock.db（既有日線）                          → 本地

用法：
  python tw_disposition.py            # 正常產生（需 twstock.db + FINMIND_TOKEN）
  python tw_disposition.py --demo     # 離線：寫入合成示範資料（給預覽/測試用）
  python tw_disposition.py --no-chips # 跳過分點（省 FinMind 流量，僅出狀態清單）

需要套件：requests、pandas（yfinance 可選，用來算大盤6日差幅）。
"""
import os
import sys
import json
import time
import sqlite3
import datetime
import argparse

import pandas as pd

try:
    import requests
except Exception:
    requests = None

try:
    import yfinance as yf
except Exception:
    yf = None

# ============================================================
#  CONFIG —— 若首次雲端執行發現清單抓不到，請看 chuzhi.json 的 diag 欄位，
#  把實際欄位名補進下面 DISP_COLS 對應，即可鎖定。
# ============================================================
DB_PATH = "twstock.db"
OUT_DIR = "site"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

HTTP_TIMEOUT = 30
CHIP_MAX_STOCKS = 80      # 分點最多抓幾檔（控制流量）
CHIP_SLEEP = 0.4          # 分點每檔間隔秒（Sponsor 額度足夠時可小）
RELEASED_WINDOW_TD = 5    # 「剛出關」回看幾個交易日
WATCH_CUM6_MIN = 25.0     # 6日累積漲幅達此值才列入 watch（%）
K1_THRESHOLD = 32.0       # 漲幅型第1款主門檻（%）

# 處置名單可能的欄位名（FinMind 版本若不同，於此擴充；診斷會印出實際欄位）
DISP_COLS = {
    "stock_id": ["stock_id", "StockID", "stock_code"],
    "start":    ["start_date", "處置開始時間", "處置起日", "begin_date", "处置开始时间", "StartDate"],
    "end":      ["end_date", "處置結束時間", "處置迄日", "stop_date", "处置结束时间", "EndDate"],
    "announce": ["date", "Date", "公告日期"],
    "measure":  ["處置措施", "處置內容", "disposal_measures", "措施", "处置措施", "Disposal"],
    "reason":   ["處置原因", "處置條件", "reason", "处置原因"],
}
# 分點表可能的欄位名
CHIP_COLS = {
    "trader_id": ["securities_trader_id", "broker_id", "trader_id"],
    "trader":    ["securities_trader", "broker", "trader_name"],
    "buy":       ["buy", "buy_volume", "Buy"],
    "sell":      ["sell", "sell_volume", "Sell"],
}


# ============================================================
#  小工具
# ============================================================
def pick_col(df, candidates):
    """從 df 欄位中找出第一個命中的候選欄名；找不到回 None。"""
    if df is None or df.empty:
        return None
    cols = set(df.columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def looks_like_date(s):
    try:
        return bool(s) and len(str(s)) >= 8 and str(s)[4] in "-/" 
    except Exception:
        return False


def detect_date_cols(df):
    """回傳值看起來像日期的欄位名清單（用於處置名單欄位偵測 fallback）。"""
    out = []
    if df is None or df.empty:
        return out
    for c in df.columns:
        try:
            sample = df[c].dropna().astype(str).head(5).tolist()
        except Exception:
            continue
        if sample and all(looks_like_date(x) for x in sample):
            out.append(c)
    return out


def next_trading_day(today_str):
    """下一交易日（僅跳過週末；不含國定假日，故為近似）。"""
    try:
        d = datetime.date.fromisoformat(today_str)
    except Exception:
        d = datetime.date.today()
    d += datetime.timedelta(days=1)
    while d.weekday() >= 5:   # 5=Sat,6=Sun
        d += datetime.timedelta(days=1)
    return d.isoformat()


def now_taipei():
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


# ============================================================
#  FinMind
# ============================================================
def finmind_get(dataset, token, max_retry=4, **params):
    if requests is None:
        raise RuntimeError("requests 未安裝")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    q = {"dataset": dataset, **params}
    wait = 8
    for _ in range(max_retry):
        try:
            resp = requests.get(FINMIND_URL, headers=headers, params=q, timeout=HTTP_TIMEOUT)
        except Exception as e:
            print(f"    [連線錯誤] {e}，{wait}s 後重試…"); time.sleep(wait); wait = min(wait * 2, 120); continue
        if resp.status_code in (402, 429):
            print(f"    [FinMind 流量上限] 等待 {wait}s…"); time.sleep(wait); wait = min(wait * 2, 120); continue
        if resp.status_code != 200:
            print(f"    [HTTP {resp.status_code}] {resp.text[:120]}"); return pd.DataFrame()
        return pd.DataFrame(resp.json().get("data", []))
    return pd.DataFrame()


# ============================================================
#  處置名單 → 解析 + 分類
# ============================================================
def parse_disposition(df, diag):
    """把 FinMind 處置名單 DataFrame 解析成標準 records:
       [{sid, start, end, round, method, measure}]。欄位名以 DISP_COLS 為主，
       找不到起迄日時用『日期樣式偵測』fallback。"""
    if df is None or df.empty:
        diag["notes"].append("處置名單為空")
        return []
    diag["disp_cols"] = list(df.columns)
    c_sid = pick_col(df, DISP_COLS["stock_id"])
    c_start = pick_col(df, DISP_COLS["start"])
    c_end = pick_col(df, DISP_COLS["end"])
    c_ann = pick_col(df, DISP_COLS["announce"])
    c_measure = pick_col(df, DISP_COLS["measure"])

    # fallback：用日期樣式找起迄日
    if not c_start or not c_end:
        dcols = [c for c in detect_date_cols(df) if c != c_ann]
        if len(dcols) >= 2:
            c_start = c_start or dcols[0]
            c_end = c_end or dcols[1]
            diag["notes"].append(f"起迄日以樣式偵測：start={c_start}, end={c_end}")
        else:
            diag["notes"].append("找不到處置起迄日欄位（請看 disp_cols 補 DISP_COLS）")

    if not c_sid:
        diag["notes"].append("找不到 stock_id 欄位")
        return []

    recs = []
    for _, row in df.iterrows():
        sid = str(row.get(c_sid, "")).strip()
        if not sid:
            continue
        start = str(row.get(c_start, "")).strip()[:10] if c_start else ""
        end = str(row.get(c_end, "")).strip()[:10] if c_end else ""
        measure = str(row.get(c_measure, "")).strip() if c_measure else ""
        # 撮合方式與次數：從 measure 文字推斷
        method = ""
        if "20" in measure or "二十" in measure:
            method = "20分盤"
        elif "5" in measure or "五" in measure:
            method = "5分盤"
        rnd = ""
        if any(k in measure for k in ("第二次", "二次", "第2次")):
            rnd = 2
        elif any(k in measure for k in ("第一次", "一次", "第1次")):
            rnd = 1
        recs.append({"sid": sid, "start": start, "end": end,
                     "round": rnd, "method": method, "measure": measure})
    # 同一檔可能多筆（多次處置）→ 保留迄日最大者為現況
    by_sid = {}
    for r in recs:
        k = r["sid"]
        if k not in by_sid or (r["end"] or "") > (by_sid[k]["end"] or ""):
            by_sid[k] = r
    diag["disp_parsed"] = len(by_sid)
    return list(by_sid.values())


def categorize(disp_recs, today, next_td):
    """把處置 records 分成 ongoing / confirmed / released。"""
    ongoing, confirmed, released = [], [], []
    for r in disp_recs:
        start, end = r.get("start", ""), r.get("end", "")
        if not start or not end:
            # 起迄不完整：若有起日且為未來，當作 confirmed；否則略過
            if start and start > today:
                confirmed.append(r)
            continue
        if start <= today <= end:
            # 處置中：計算第幾天 / 總天數（以日曆近似；實務為交易日）
            r2 = dict(r)
            r2["release"] = end
            r2["d2r"] = max(0, _date_diff(today, end))
            tot = _date_diff(start, end) + 1
            r2["day_total"] = tot
            r2["day_n"] = min(tot, _date_diff(start, today) + 1)
            ongoing.append(r2)
        elif start > today:
            # 未來起算 → 確定進處置（含明日）
            r2 = dict(r)
            r2["days"] = _date_diff(start, end) + 1
            confirmed.append(r2)
        elif end < today:
            # 已出關：是否在回看窗內
            since = _date_diff(end, today)
            if 0 < since <= RELEASED_WINDOW_TD + 2:   # +2 容忍週末
                r2 = dict(r)
                r2["since"] = since
                released.append(r2)
    # confirmed 以起日排序，明日的排前面
    confirmed.sort(key=lambda x: x.get("start", ""))
    ongoing.sort(key=lambda x: x.get("d2r", 999))
    released.sort(key=lambda x: x.get("since", 999))
    return ongoing, confirmed, released


def _date_diff(a, b):
    """b - a，回傳日數（字串 ISO 日期）。失敗回 0。"""
    try:
        da = datetime.date.fromisoformat(a[:10])
        db = datetime.date.fromisoformat(b[:10])
        return (db - da).days
    except Exception:
        return 0


# ============================================================
#  watch（漲幅型估計，純本地價格）
# ============================================================
def load_recent_closes(con, ndays=9):
    """回傳 {sid: [(date, close), ...] 由舊到新}，只取最近 ndays 個交易日。"""
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM price ORDER BY date DESC LIMIT ?", (ndays,))]
    if not dates:
        return {}, ""
    dates = sorted(dates)
    qmarks = ",".join("?" * len(dates))
    rows = con.execute(
        f"SELECT stock_id, date, close FROM price WHERE date IN ({qmarks})", dates).fetchall()
    by = {}
    for sid, d, c in rows:
        by.setdefault(sid, []).append((d, c))
    for sid in by:
        by[sid].sort(key=lambda x: x[0])
    return by, dates[-1]


def twii_6d_change():
    """大盤近6個交易日累積漲跌%（用 yfinance ^TWII）。失敗回 0。"""
    if yf is None:
        return 0.0
    try:
        h = yf.Ticker("^TWII").history(period="1mo")
        c = h["Close"].dropna().tolist()
        if len(c) >= 7:
            return (c[-1] / c[-7] - 1.0) * 100.0
    except Exception:
        pass
    return 0.0


def compute_watch(closes_by_sid, names, idx6, disp_sids):
    """用最近收盤算漲幅型估計，挑出接近/已達第1款者。"""
    out = []
    for sid, seq in closes_by_sid.items():
        if sid in disp_sids:
            continue
        if len(seq) < 7:
            continue
        closes = [c for _, c in seq if c is not None]
        if len(closes) < 7:
            continue
        last = closes[-1]; prev = closes[-2]; base6 = closes[-7]
        if not base6 or base6 <= 0:
            continue
        cum6 = (last / base6 - 1.0) * 100.0
        if cum6 < WATCH_CUM6_MIN:
            continue
        chg = (last / prev - 1.0) * 100.0 if prev else None
        diff6 = cum6 - idx6          # 與大盤差幅（粗略，未含同類股）
        gap32 = K1_THRESHOLD - cum6
        # 明日入關價(估)：明日6日窗 base = closes[-6]，門檻用 32% + 大盤差幅近似
        base_next = closes[-6]
        entry_next = round(base_next * (1 + (K1_THRESHOLD + idx6) / 100.0), 2) if base_next else None
        light = "red" if cum6 >= K1_THRESHOLD else ("amber" if cum6 >= WATCH_CUM6_MIN else "green")
        note = ""
        if cum6 >= K1_THRESHOLD:
            note = "今日已達漲幅型第1款，連續達標即累計處置"
        elif gap32 <= 3:
            note = "已逼近第1款門檻，明日續強恐列注意"
        out.append({
            "sid": sid, "name": names.get(sid, ""), "mkt": "",
            "close": round(last, 2), "chg": round(chg, 2) if chg is not None else None,
            "cum6": round(cum6, 1), "gap32": round(gap32, 1),
            "entry_next": entry_next, "light": light, "note": note,
            "_diff6": round(diff6, 1),
        })
    # 已達第1款(紅) 優先，其次接近(差距小) 優先
    out.sort(key=lambda x: (0 if x["light"] == "red" else 1, x["gap32"]))
    return out


# ============================================================
#  分點 → 主力買賣超 / 集中度CC15
# ============================================================
def chip_metrics(df):
    """從單檔單日分點 DataFrame 算 (主力買賣超張, CC15%)。
       主力買賣超 = 前15買超分點淨額 + 前15賣超分點淨額（張）。
       CC15 = (前15買超 − |前15賣超|) ÷ 當日總買量 × 100%。"""
    if df is None or df.empty:
        return None, None
    c_buy = pick_col(df, CHIP_COLS["buy"])
    c_sell = pick_col(df, CHIP_COLS["sell"])
    c_tid = pick_col(df, CHIP_COLS["trader_id"]) or pick_col(df, CHIP_COLS["trader"])
    if not c_buy or not c_sell:
        return None, None
    d = df.copy()
    d[c_buy] = pd.to_numeric(d[c_buy], errors="coerce").fillna(0)
    d[c_sell] = pd.to_numeric(d[c_sell], errors="coerce").fillna(0)
    # 以券商彙總（同券商多列價位）
    if c_tid:
        g = d.groupby(c_tid, as_index=False)[[c_buy, c_sell]].sum()
    else:
        g = d
    g["net"] = g[c_buy] - g[c_sell]          # 股
    total_buy = g[c_buy].sum()
    if total_buy <= 0:
        return None, None
    pos = g[g["net"] > 0].nlargest(15, "net")["net"].sum()
    neg = g[g["net"] < 0].nsmallest(15, "net")["net"].sum()   # 負值
    main_force = (pos + neg) / 1000.0                          # 張
    cc15 = (pos - abs(neg)) / total_buy * 100.0
    return round(main_force, 0), round(cc15, 1)


def fetch_chips(token, sids, date, diag, sleep=CHIP_SLEEP):
    """對給定股票清單抓當日分點並計算指標。回傳 {sid: (mf, cc15)}。"""
    res = {}
    if not token:
        diag["notes"].append("未設 FINMIND_TOKEN，略過分點")
        return res
    sids = list(sids)[:CHIP_MAX_STOCKS]
    ok = 0
    for sid in sids:
        try:
            df = finmind_get("TaiwanStockTradingDailyReport", token,
                             data_id=sid, start_date=date, end_date=date)
            mf, cc = chip_metrics(df)
            if mf is not None:
                res[sid] = (mf, cc); ok += 1
        except Exception as e:
            print(f"    分點 {sid} 失敗：{e}")
        time.sleep(sleep)
    diag["chip_ok"] = ok
    diag["notes"].append(f"分點成功 {ok}/{len(sids)} 檔")
    return res


def apply_chips(lst, chips):
    for r in lst:
        mf_cc = chips.get(r["sid"])
        if mf_cc:
            r["mf"], r["cc15"] = mf_cc[0], mf_cc[1]
        else:
            r.setdefault("mf", None); r.setdefault("cc15", None)
    return lst


# ============================================================
#  組裝 + 輸出
# ============================================================
def attach_market_names(con, lists, names, mkts):
    for lst in lists:
        for r in lst:
            if not r.get("name"):
                r["name"] = names.get(r["sid"], "")
            if not r.get("mkt"):
                r["mkt"] = mkts.get(r["sid"], "")


def build_payload(today, next_td, watch, confirmed, ongoing, released, diag):
    return {
        "gentime": now_taipei(),
        "today": today,
        "next_td": next_td,
        "counts": {"watch": len(watch), "confirmed": len(confirmed),
                   "ongoing": len(ongoing), "released": len(released)},
        "diag": diag,
        "watch": watch,
        "confirmed": confirmed,
        "ongoing": ongoing,
        "released": released,
    }


def write_outputs(out_dir, payload):
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
    with open(os.path.join(out_dir, "data", "chuzhi.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    with open(os.path.join(out_dir, "chuzhi.html"), "w", encoding="utf-8") as f:
        f.write(CHUZHI_HTML)
    print(f"已寫出 {out_dir}/chuzhi.html 與 {out_dir}/data/chuzhi.json "
          f"（watch {payload['counts']['watch']}・確定 {payload['counts']['confirmed']}・"
          f"處置中 {payload['counts']['ongoing']}・出關 {payload['counts']['released']}）")


# ============================================================
#  示範資料（離線）
# ============================================================
DEMO_PAYLOAD = None  # 於 __main__ 由 demo json 載入（若存在），否則內建


def make_demo():
    today = "2026-06-27"
    diag = {"notes": ["[示範模式] 合成資料，非真實行情"], "disp_cols": [], "chip_ok": 0}
    watch = [
        {"sid": "6741", "name": "91APP*-KY", "mkt": "上市", "close": 214.5, "chg": 6.83,
         "cum6": 29.4, "gap32": 2.6, "entry_next": 221.0, "light": "amber", "mf": 1820, "cc15": 18.7,
         "note": "已逼近第1款門檻，明日續強恐列注意"},
        {"sid": "4129", "name": "聯合", "mkt": "上市", "close": 58.9, "chg": 9.92,
         "cum6": 33.8, "gap32": -1.8, "entry_next": 55.7, "light": "red", "mf": 640, "cc15": 11.2,
         "note": "今日已達漲幅型第1款"},
        {"sid": "3083", "name": "網龍", "mkt": "上櫃", "close": 102.0, "chg": 3.55,
         "cum6": 26.1, "gap32": 5.9, "entry_next": 108.5, "light": "amber", "mf": -210, "cc15": -3.4,
         "note": "主力轉賣超，留意假突破"},
    ]
    confirmed = [
        {"sid": "2618", "name": "長榮航", "mkt": "上市", "close": 48.6, "chg": 9.95, "round": 1,
         "method": "5分盤", "start": "2026-06-30", "end": "2026-07-11", "days": 10, "mf": 3200, "cc15": 22.5},
        {"sid": "1815", "name": "富喬", "mkt": "上市", "close": 31.2, "chg": 9.86, "round": 2,
         "method": "20分盤", "start": "2026-06-30", "end": "2026-07-11", "days": 10, "mf": 850, "cc15": 15.1},
    ]
    ongoing = [
        {"sid": "2606", "name": "裕民", "mkt": "上市", "close": 72.4, "chg": -3.21, "round": 1,
         "method": "5分盤", "start": "2026-06-23", "end": "2026-07-04", "day_n": 3, "day_total": 10,
         "release": "2026-07-04", "d2r": 7, "mf": 1500, "cc15": 19.8},
        {"sid": "5483", "name": "中美晶", "mkt": "上櫃", "close": 188.0, "chg": 2.17, "round": 1,
         "method": "5分盤", "start": "2026-06-20", "end": "2026-07-01", "day_n": 6, "day_total": 10,
         "release": "2026-07-01", "d2r": 4, "mf": -430, "cc15": -5.2},
        {"sid": "4736", "name": "泰博", "mkt": "上市", "close": 255.5, "chg": 0.0, "round": 2,
         "method": "20分盤", "start": "2026-06-18", "end": "2026-07-03", "day_n": 8, "day_total": 12,
         "release": "2026-07-03", "d2r": 6, "mf": 920, "cc15": 24.1},
    ]
    released = [
        {"sid": "3035", "name": "智原", "mkt": "上市", "close": 312.0, "chg": -5.45,
         "end": "2026-06-24", "since": 3, "perf": -8.6, "mf": -1200, "cc15": -7.1},
        {"sid": "8069", "name": "元太", "mkt": "上櫃", "close": 205.0, "chg": 4.06,
         "end": "2026-06-26", "since": 1, "perf": 3.2, "mf": 2100, "cc15": 16.4},
    ]
    return build_payload(today, next_trading_day(today), watch, confirmed, ongoing, released, diag)


# ============================================================
#  主程式
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="離線：寫入合成示範資料")
    ap.add_argument("--no-chips", action="store_true", help="跳過分點（省 FinMind 流量）")
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()

    if args.demo:
        write_outputs(args.out, make_demo())
        return

    if not os.path.exists(DB_PATH):
        print(f"找不到 {DB_PATH}，改寫示範資料（請先讓 screener 建好 DB）。")
        write_outputs(args.out, make_demo())
        return

    diag = {"notes": [], "disp_cols": [], "chip_ok": 0}
    con = sqlite3.connect(DB_PATH)
    names = {r[0]: r[1] for r in con.execute("SELECT stock_id, name FROM stock")}
    mkts = {r[0]: r[1] for r in con.execute("SELECT stock_id, market FROM stock")}

    closes_by_sid, today = load_recent_closes(con)
    if not today:
        today = datetime.date.today().isoformat()
    next_td = next_trading_day(today)

    # 1) 處置名單（抓最近 ~45 天公告，涵蓋處置中／即將出關／剛出關）
    disp_recs = []
    try:
        disp_start = (datetime.date.fromisoformat(today) - datetime.timedelta(days=45)).isoformat()
        df = finmind_get("TaiwanStockDispositionSecuritiesPeriod", FINMIND_TOKEN, start_date=disp_start)
        diag["notes"].append(f"處置名單 {0 if df is None else len(df)} 筆")
        disp_recs = parse_disposition(df, diag)
    except Exception as e:
        diag["notes"].append(f"處置名單抓取失敗：{e}")

    disp_sids = set(r["sid"] for r in disp_recs)
    ongoing, confirmed, released = categorize(disp_recs, today, next_td)

    # 2) watch（漲幅型估計）
    idx6 = twii_6d_change()
    if idx6:
        diag["notes"].append(f"大盤6日差幅 {idx6:.1f}% 已套用")
    watch = compute_watch(closes_by_sid, names, idx6, disp_sids)

    # 3) 補今日漲跌與收盤到 ongoing/confirmed/released（從 DB）
    last_close = {sid: seq[-1][1] for sid, seq in closes_by_sid.items() if seq}
    prev_close = {sid: seq[-2][1] for sid, seq in closes_by_sid.items() if len(seq) >= 2}
    for lst in (ongoing, confirmed, released):
        for r in lst:
            sid = r["sid"]
            lc = last_close.get(sid); pc = prev_close.get(sid)
            r["close"] = round(lc, 2) if lc else None
            r["chg"] = round((lc / pc - 1) * 100, 2) if lc and pc else None
    # released 的「出關後%」：自迄日收盤起算到最新
    for r in released:
        seq = closes_by_sid.get(r["sid"], [])
        end_c = next((c for d, c in seq if d >= r.get("end", "")), None)
        last_c = seq[-1][1] if seq else None
        r["perf"] = round((last_c / end_c - 1) * 100, 2) if end_c and last_c else None

    attach_market_names(con, [watch, confirmed, ongoing, released], names, mkts)

    # 4) 分點（對 ongoing+confirmed+watch 子集）
    if not args.no_chips:
        targets = list(dict.fromkeys(
            [r["sid"] for r in ongoing] + [r["sid"] for r in confirmed]
            + [r["sid"] for r in released] + [r["sid"] for r in watch]))
        chips = fetch_chips(FINMIND_TOKEN, targets, today, diag)
        for lst in (watch, confirmed, ongoing, released):
            apply_chips(lst, chips)
    else:
        for lst in (watch, confirmed, ongoing, released):
            for r in lst:
                r.setdefault("mf", None); r.setdefault("cc15", None)

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

  /* 可橫向捲動的分頁列 */
  .cztabs{display:flex; gap:6px; margin:14px 0; background:var(--card); padding:5px; border-radius:11px;
    border:1px solid var(--border); overflow-x:auto; -webkit-overflow-scrolling:touch; scrollbar-width:none;}
  .cztabs::-webkit-scrollbar{display:none;}
  .czt{flex:0 0 auto; background:transparent; color:var(--muted); border:none; border-radius:8px;
    padding:9px 13px; font-size:13.5px; font-weight:700; cursor:pointer; white-space:nowrap;}
  .czt.on{background:var(--amber-s); color:var(--amber);}

  .pane{animation:fade .2s ease;}
  @keyframes fade{from{opacity:0; transform:translateY(4px);}to{opacity:1; transform:none;}}

  /* 統計卡 */
  .stats{display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-bottom:14px;}
  .stat{background:var(--card); border:1px solid var(--border); border-radius:11px; padding:12px 10px; text-align:center;}
  .stat .n{font-size:24px; font-weight:800; line-height:1;}
  .stat .l{font-size:11px; color:var(--muted); margin-top:6px;}
  .stat.w .n{color:var(--amber);} .stat.c .n{color:var(--up);}
  .stat.o .n{color:var(--blue);} .stat.r .n{color:var(--down);}

  .sech{font-size:13px; font-weight:700; color:var(--muted); margin:18px 2px 9px; display:flex; align-items:center; gap:7px;}
  .sech .pill{font-size:11px; font-weight:600; color:var(--dim); background:var(--card2); border:1px solid var(--border); padding:2px 8px; border-radius:99px;}

  /* 個股卡 */
  .card{background:var(--card); border:1px solid var(--border); border-radius:13px; padding:13px 15px; margin-bottom:9px;}
  .card .top{display:flex; align-items:baseline; gap:9px;}
  .card .sid{font-size:16px; font-weight:800; color:var(--text); font-variant-numeric:tabular-nums;}
  .card .nm{font-size:14px; color:var(--muted); flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
  .card .mkt{font-size:10.5px; color:var(--dim); border:1px solid var(--border); border-radius:5px; padding:1px 6px;}
  .card .px{text-align:right;}
  .card .px .p{font-size:15px; font-weight:700; font-variant-numeric:tabular-nums;}
  .card .px .c{font-size:12px; font-weight:600; font-variant-numeric:tabular-nums;}
  .card .meta{display:flex; flex-wrap:wrap; gap:6px 14px; margin-top:10px; font-size:12.5px; color:var(--muted);}
  .card .meta b{color:var(--text); font-weight:700; font-variant-numeric:tabular-nums;}
  .card .note{font-size:12px; color:var(--dim); margin-top:8px; line-height:1.5;}
  .up{color:var(--up);} .down{color:var(--down);} .flat{color:var(--muted);}

  .dot{display:inline-block; width:9px; height:9px; border-radius:99px; margin-right:2px; vertical-align:middle;}
  .dot.red{background:var(--up); box-shadow:0 0 7px rgba(255,77,79,.7);}
  .dot.amber{background:var(--amber); box-shadow:0 0 7px rgba(245,165,36,.7);}
  .dot.green{background:var(--down);}

  /* 進度條 */
  .prog{height:7px; background:#0e1626; border-radius:5px; overflow:hidden; margin:9px 0 3px; flex:1;}
  .progf{height:100%; background:linear-gradient(90deg,#4d9fff,#27c4dc); border-radius:5px;}

  .chip{display:inline-block; font-size:11px; font-weight:700; padding:2px 8px; border-radius:6px; margin-right:6px;}
  .chip.m5{background:var(--red-s); color:var(--up);} .chip.m20{background:var(--purple-s); color:var(--purple);}
  .chip.r1{background:var(--amber-s); color:var(--amber);} .chip.r2{background:var(--red-s); color:var(--up);}

  .empty{color:var(--dim); font-size:13.5px; text-align:center; padding:34px 12px; line-height:1.7;}
  .note{font-size:12px; color:var(--dim); line-height:1.65; margin-top:14px; padding:13px 15px;
    background:var(--card); border:1px solid var(--border); border-radius:11px;}
  .note b{color:var(--muted);}

  /* 教學 / 規則 */
  .doc{background:var(--card); border:1px solid var(--border); border-radius:13px; padding:17px 18px; line-height:1.72; font-size:14px;}
  .doc h3{font-size:15.5px; margin:20px 0 9px; color:var(--amber);}
  .doc h3:first-child{margin-top:2px;}
  .doc p{margin:8px 0; color:var(--text);}
  .doc .lead{color:var(--muted); font-size:13.5px;}
  .doc ul{margin:8px 0; padding-left:20px;}
  .doc li{margin:6px 0;}
  .doc .k{color:var(--amber); font-weight:700;}
  .doc .warn{background:var(--red-s); border:1px solid rgba(255,77,79,.3); border-radius:9px; padding:11px 13px; margin:12px 0; font-size:13px; color:#ffd9da;}
  .doc .tip{background:var(--blue-s); border:1px solid rgba(77,159,255,.25); border-radius:9px; padding:11px 13px; margin:12px 0; font-size:13px;}
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

  <!-- 總覽 -->
  <div class="pane" id="p-ov">
    <div class="stats">
      <div class="stat w"><div class="n num" id="cnt-w">—</div><div class="l">即將進處置</div></div>
      <div class="stat c"><div class="n num" id="cnt-c">—</div><div class="l">明日確定</div></div>
      <div class="stat o"><div class="n num" id="cnt-o">—</div><div class="l">處置中</div></div>
      <div class="stat r"><div class="n num" id="cnt-r">—</div><div class="l">剛出關</div></div>
    </div>
    <div class="note">
      <b>燈號</b>：<span class="dot red"></span>紅＝今日已觸發漲幅型（第1款，最危險）　<span class="dot amber"></span>黃＝接近門檻（6日漲幅 25%~32%）　<span class="dot green"></span>綠＝安全。<br>
      <b>主力</b>＝前15大買超分點淨額（張）；<b>集中度</b>＝(前15買超−前15賣超)÷區間量×100%，正值代表籌碼握在少數人手中（資料：FinMind Sponsor 分點，T+1 盤後）。<br>
      「即將進處置」為本站用收盤價自算的<b>漲幅型估計</b>（未計入同類股差幅），僅供提前留意；實際是否列注意/處置以證交所、櫃買公告為準。
    </div>
    <div class="note" id="diagbox" style="display:none"></div>
  </div>

  <!-- 即將進處置 -->
  <div class="pane hidden" id="p-watch">
    <div class="sech">📈 即將/可能進處置 <span class="pill">漲幅型估計・盤後自算</span></div>
    <div id="list-watch"></div>
    <div class="note">判讀：6日累積漲幅越接近 <b>32%</b>（或高價股 25%＋起迄價差 50 元）越可能被列注意；連續達標再累計即進處置。<b>明日入關價（估）</b>＝跌破此價可暫時避開漲幅型注意。此清單未含週轉率/當沖比等其他款別，請搭配主看板量能與籌碼一起看。</div>
  </div>

  <!-- 明日確定 -->
  <div class="pane hidden" id="p-confirm">
    <div class="sech">🔒 下一交易日確定進入處置 <span class="pill">FinMind 處置公告</span></div>
    <div id="list-confirm"></div>
    <div class="note">這些個股已公告自下一交易日起進入分盤處置。<b>5分盤</b>＝第一次處置（單筆≥10張或累積≥30張預收款券）；<b>20分盤</b>＝第二次以上（不論張數全額預收、停信用）。處置期間<b>不可現股當沖</b>。</div>
  </div>

  <!-- 處置中 -->
  <div class="pane hidden" id="p-ongoing">
    <div class="sech">⛓️ 處置中（坐牢） <span class="pill">分盤交易期</span></div>
    <div id="list-ongoing"></div>
    <div class="note">觀察「上升月線(MA20)是否守住」與「主力是否續抱（集中度未轉負）」。量縮、籌碼安定者出關較易噴出；主力撤退（集中度轉負）則再便宜都不接。<b>絕不參與出關當天的開高走低博弈</b>。</div>
  </div>

  <!-- 剛出關 -->
  <div class="pane hidden" id="p-released">
    <div class="sech">🕊️ 剛脫離處置 <span class="pill">近5個交易日內出關</span></div>
    <div id="list-released"></div>
    <div class="note">出關首日常見<b>開高走低</b>（流動性恢復＝獲利了結賣壓）。當沖：開高爆量收長上影、跌破開盤價可順勢偏空（用股期/融券）。波段：須等<b>帶量站回前高</b>才是明確續抱買點，不可在出關日無腦追多。「出關後%」為自出關日收盤起算的漲跌幅。</div>
  </div>

  <!-- 實戰教學 -->
  <div class="pane hidden" id="p-teach">
    <div class="doc">
      <h3>一句話：處置股在玩什麼？</h3>
      <p class="lead">股票短期漲太兇（或量/當沖太誇張）會被交易所「關起來」分盤交易、限制當沖。流動性被抽乾後，籌碼鎖死，股價容易<b>暴漲暴跌</b>。我們不賭它被關，而是抓「進處置前、處置中、出關」三個時點的大波動。</p>

      <h3>五種狀態，你各該做什麼</h3>
      <table>
        <tr><th>狀態</th><th>白話</th><th>怎麼做</th></tr>
        <tr><td class="k">即將進處置</td><td>漲幅接近門檻、聽牌中</td><td>賭公告前最後一漲：須月線向上、主力沒跑。公告後可能跳水，控好部位</td></tr>
        <tr><td class="k">明日確定</td><td>明天起被關</td><td>波段客可尾盤卡位「越關越大尾」標的；當沖客準備出關日的反向操作</td></tr>
        <tr><td class="k">處置中</td><td>分盤坐牢</td><td>低接「拉回月線且主力沒跑」；嚴禁現股當沖；跌破月線或主力撤退就走</td></tr>
        <tr><td class="k">剛出關</td><td>恢復正常交易</td><td>首日多開高走低→偏空；要續抱等「帶量過前高」再進</td></tr>
        <tr><td class="k">逃開處置</td><td>差一點壓回沒被關</td><td>少了分盤限制，短線資金常回流補漲，籌碼好可追</td></tr>
      </table>

      <h3>兩個核心買點（白話版）</h3>
      <div class="step"><div class="no">1</div><div class="tx"><b>浪子回頭</b>：強勢股拉回到月線（20日均線）附近就止跌、而且量縮、主力沒走。＝回測月線±2%、量縮到平常的3成以下、近5/10日籌碼集中度仍正。這是相對低風險的進場點。</div></div>
      <div class="step"><div class="no">2</div><div class="tx"><b>深蹲蓄力</b>：處置期間因為買盤太少，被一根大單殺到接近跌停（單日跌約9%以上），但盤後一看主力其實沒在倒貨（集中度沒轉負）＝恐慌錯殺，容易反彈。</div></div>

      <h3>進階：雙刀對鎖（規避大盤風險）</h3>
      <p>做多處置股時，同時在「另一檔走勢高度連動、且有股票期貨」的標的放空相同金額，鎖住價差、避開大盤突然崩的風險。連動度用日收盤算相關係數 ≥ 0.85 來篩。</p>

      <h3>鐵則（賠錢都從破戒開始）</h3>
      <div class="warn">
        ① <b>止損機械化</b>：收盤跌破月線超過3%，或主力集中度連3日轉負 → 隔天開盤無條件出。<br>
        ② <b>不玩出關當天</b>：出關首日開高走低機率高，獲利先落袋，別貪。<br>
        ③ <b>當沖税費會吃光價差</b>：高頻沖處置股，手續費＋證交稅＋分盤滑價常讓你做白工。<br>
        ④ <b>處置期間禁現股當沖</b>：要當沖只能等出關日或用對應股期。<br>
        ⑤ <b>大盤過熱要縮手</b>：當天漲停家數＞20、或處置股暴增時，是資金末段訊號，降部位。
      </div>

      <h3>一天的節奏（簡版）</h3>
      <div class="step"><div class="no">早</div><div class="tx">盤前看「今日出關」名單→列出關日放空候選；看「明日必關」→列尾盤觀察。</div></div>
      <div class="step"><div class="no">盤</div><div class="tx">出關股開高爆量收長上影、跌破開盤價→偏空；自選處置股急殺但主力沒跑→記下準備低接。</div></div>
      <div class="step"><div class="no">尾</div><div class="tx">波段低接在13:25前分批掛限價（先放10%資金）；確認月線止跌帶量紅K再加碼到上限20%。</div></div>

      <p class="discl">本頁為交易紀律整理，非投資建議。處置股屬高風險投機，務必先用小部位與歷史回測驗證後再執行。所有門檻數字為對公開教學的量化重構，需依你自己的回測校準。</p>
    </div>
  </div>

  <!-- 規則說明 -->
  <div class="pane hidden" id="p-rule">
    <div class="doc">
      <h3>注意股 vs 處置股，差在哪？</h3>
      <p><b>注意股</b>：盤後計算，達標就公告，<b>只是提醒、交易方式不變</b>。<br>
      <b>處置股</b>：注意累積到一定次數後升級，<b>真的有交易限制</b>（改分盤、要預收錢、禁當沖）。先注意、再處置。</p>

      <h3>怎樣會被「注意」？（白話門檻）</h3>
      <ul>
        <li><span class="k">漲太快</span>：最近6個交易日累積漲跌幅 <b>超過32%</b>（且明顯比大盤、同類股強）；或超過25%且這6天頭尾價差達50元（多為高價股）。跌太快同理。</li>
        <li><span class="k">量爆掉</span>：當天量是近60日均量的5倍以上。</li>
        <li><span class="k">週轉率太高</span>：當天周轉率＞10%、或近6日累積＞50%。</li>
        <li><span class="k">當沖太兇</span>：近6日與當日當沖佔比都＞60%（這項會讓處置天數拉長到12天）。</li>
        <li><span class="k">券資比、本益比異常</span>等其他款別。</li>
      </ul>

      <h3>怎樣會被「處置」？</h3>
      <p>近期累積到門檻就升級處置，例如：<b>連續3天</b>達漲幅型；或<b>連5天 / 近10天有6天 / 近30天有12天</b>達前述任一款。</p>

      <h3>第一次 vs 第二次處置</h3>
      <table>
        <tr><th></th><th>第一次處置</th><th>第二次以上</th></tr>
        <tr><td>撮合</td><td>約每 <b>5分鐘</b> 一次</td><td>約每 <b>20分鐘</b> 一次</td></tr>
        <tr><td>要先付錢嗎</td><td>單筆≥10張或累積≥30張才預收</td><td><b>不論張數全額預收</b>（等於全額交割）</td></tr>
        <tr><td>信用交易</td><td>視個案收足</td><td>停融資、融券保證金100%</td></tr>
        <tr><td>天數</td><td>10個交易日（當沖太兇→12天）</td><td>10~12個交易日</td></tr>
      </table>

      <h3>處置期間的限制</h3>
      <ul>
        <li><b>不能現股當沖、不能資券當沖</b>（當沖客被擋在門外）。</li>
        <li>只能掛<b>限價單</b>，分盤集合競價，每5秒揭示模擬價量與五檔。</li>
        <li>對應的<b>股票期貨不受限</b>（保證金會調高）——這是被關時唯一能即時操作/對鎖的工具。</li>
      </ul>

      <h3>出關</h3>
      <p>處置天數到了就恢復正常交易（出關）。出關當天流動性瞬間恢復，常引發獲利了結賣壓，<b>開高走低</b>很常見。</p>

      <h3>上市 / 上櫃 / 興櫃</h3>
      <p>上市（證交所）與上櫃（櫃買）標準幾乎一樣。<b>興櫃完全不同</b>：沒有漲跌幅、用議價，靠「均價比前一日差50%就熔斷停到收盤」，波動極端，新手別碰。</p>

      <p class="discl">以上為簡化說明，實際量化門檻以證交所「公布或通知注意交易資訊暨處置作業要點」與櫃買中心最新公告為準。</p>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));

// 台股紅漲綠跌
function pctSpan(v, withSign=true){
  if(v==null||v===""||isNaN(v)) return '<span class="flat">—</span>';
  const n=Number(v); const cls=n>0?"up":(n<0?"down":"flat");
  const s=(withSign&&n>0?"+":"")+n.toFixed(2)+"%";
  return `<span class="${cls}">${s}</span>`;
}
function priceTxt(p){ return (p==null||p==="")?"—":Number(p).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}); }
function dotFor(light){ const c=light==="red"?"red":(light==="amber"?"amber":"green"); return `<span class="dot ${c}"></span>`; }
function mfChip(mf,cc){
  let out="";
  if(mf!=null&&mf!=="") out+=`主力 <b class="${Number(mf)>0?'up':(Number(mf)<0?'down':'')}">${Number(mf)>0?'+':''}${Math.round(Number(mf)).toLocaleString()}</b> 張　`;
  if(cc!=null&&cc!=="") out+=`集中度 <b class="${Number(cc)>0?'up':(Number(cc)<0?'down':'')}">${Number(cc).toFixed(1)}%</b>`;
  return out;
}
function headTop(r){
  return `<div class="top">
    <span class="sid num">${esc(r.sid)}</span>
    <span class="nm">${esc(r.name||"")}</span>
    ${r.mkt?`<span class="mkt">${esc(r.mkt)}</span>`:""}
    <span class="px"><div class="p num">${priceTxt(r.close)}</div><div class="c">${pctSpan(r.chg)}</div></span>
  </div>`;
}
function emptyBox(msg){ return `<div class="empty">${msg}</div>`; }

function renderWatch(arr){
  const el=$("list-watch");
  if(!arr||!arr.length){ el.innerHTML=emptyBox("目前沒有接近處置門檻的個股。<br>（市場較冷或漲勢個股不足）"); return; }
  el.innerHTML=arr.map(r=>`<div class="card">
    ${headTop(r)}
    <div class="meta">
      <span>${dotFor(r.light)}6日漲幅 <b>${r.cum6==null?"—":Number(r.cum6).toFixed(1)+"%"}</b></span>
      <span>距32% <b>${r.gap32==null?"—":(Number(r.gap32)>0?Number(r.gap32).toFixed(1)+"%":"已超過")}</b></span>
      <span>明日入關價(估) <b>${priceTxt(r.entry_next)}</b></span>
    </div>
    ${(r.mf!=null||r.cc15!=null)?`<div class="meta"><span>${mfChip(r.mf,r.cc15)}</span></div>`:""}
    ${r.note?`<div class="note">${esc(r.note)}</div>`:""}
  </div>`).join("");
}

function renderConfirm(arr){
  const el=$("list-confirm");
  if(!arr||!arr.length){ el.innerHTML=emptyBox("下一交易日沒有新進處置的個股。"); return; }
  el.innerHTML=arr.map(r=>`<div class="card">
    ${headTop(r)}
    <div class="meta">
      <span>${methodChip(r.method)}${roundChip(r.round)}</span>
      <span>處置期間 <b>${esc(r.start||"—")} ~ ${esc(r.end||"—")}</b></span>
      ${r.days?`<span>共 <b>${esc(r.days)}</b> 個交易日</span>`:""}
    </div>
    ${(r.mf!=null||r.cc15!=null)?`<div class="meta"><span>${mfChip(r.mf,r.cc15)}</span></div>`:""}
  </div>`).join("");
}

function renderOngoing(arr){
  const el=$("list-ongoing");
  if(!arr||!arr.length){ el.innerHTML=emptyBox("目前沒有處置中的個股。"); return; }
  el.innerHTML=arr.map(r=>{
    const pct=(r.day_n&&r.day_total)?Math.max(0,Math.min(100,Math.round(r.day_n/r.day_total*100))):0;
    return `<div class="card">
      ${headTop(r)}
      <div class="meta">
        <span>${methodChip(r.method)}${roundChip(r.round)}</span>
        <span>出關日 <b>${esc(r.release||"—")}</b>${r.d2r!=null?`（剩 <b>${esc(r.d2r)}</b> 天）`:""}</span>
      </div>
      <div style="display:flex; align-items:center; gap:10px;">
        <div class="prog"><div class="progf" style="width:${pct}%"></div></div>
        <span class="num" style="font-size:12px;color:var(--muted)">${r.day_n||"?"}/${r.day_total||"?"}</span>
      </div>
      ${(r.mf!=null||r.cc15!=null)?`<div class="meta"><span>${mfChip(r.mf,r.cc15)}</span></div>`:""}
    </div>`;
  }).join("");
}

function renderReleased(arr){
  const el=$("list-released");
  if(!arr||!arr.length){ el.innerHTML=emptyBox("近5個交易日內沒有出關的個股。"); return; }
  el.innerHTML=arr.map(r=>`<div class="card">
    ${headTop(r)}
    <div class="meta">
      <span>出關日 <b>${esc(r.end||"—")}</b>${r.since!=null?`（已 <b>${esc(r.since)}</b> 天）`:""}</span>
      <span>出關後 ${pctSpan(r.perf)}</span>
    </div>
    ${(r.mf!=null||r.cc15!=null)?`<div class="meta"><span>${mfChip(r.mf,r.cc15)}</span></div>`:""}
  </div>`).join("");
}

function methodChip(m){
  if(!m) return "";
  if(String(m).indexOf("20")>=0) return `<span class="chip m20">20分盤</span>`;
  if(String(m).indexOf("5")>=0)  return `<span class="chip m5">5分盤</span>`;
  return `<span class="chip m5">${esc(m)}</span>`;
}
function roundChip(r){
  if(r==null||r==="") return "";
  const n=Number(r);
  if(n>=2) return `<span class="chip r2">第${n}次</span>`;
  return `<span class="chip r1">第1次</span>`;
}

function switchTab(p){
  document.querySelectorAll(".czt").forEach(b=>b.classList.toggle("on", b.dataset.p===p));
  ["ov","watch","confirm","ongoing","released","teach","rule"].forEach(x=>{
    const node=$("p-"+x); if(node) node.classList.toggle("hidden", x!==p);
  });
  try{ window.scrollTo({top:0,behavior:"smooth"}); }catch(e){ window.scrollTo(0,0); }
}
document.querySelectorAll(".czt").forEach(b=>b.addEventListener("click",()=>switchTab(b.dataset.p)));

async function boot(){
  let d=null;
  try{ const r=await fetch("data/chuzhi.json",{cache:"default"}); if(r.ok) d=await r.json(); }catch(e){}
  if(!d){
    $("today").textContent="資料尚未產生";
    ["watch","confirm","ongoing","released"].forEach(k=>{ const el=$("list-"+k); if(el) el.innerHTML=emptyBox("尚未取得處置資料。<br>請先在 GitHub Actions 執行一次工作流程產生 data/chuzhi.json。"); });
    return;
  }
  $("today").textContent=d.today||"—";
  $("gentime").textContent=d.gentime||"—";
  const c=d.counts||{};
  $("cnt-w").textContent=c.watch!=null?c.watch:((d.watch||[]).length);
  $("cnt-c").textContent=c.confirmed!=null?c.confirmed:((d.confirmed||[]).length);
  $("cnt-o").textContent=c.ongoing!=null?c.ongoing:((d.ongoing||[]).length);
  $("cnt-r").textContent=c.released!=null?c.released:((d.released||[]).length);
  renderWatch(d.watch||[]);
  renderConfirm(d.confirmed||[]);
  renderOngoing(d.ongoing||[]);
  renderReleased(d.released||[]);
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
