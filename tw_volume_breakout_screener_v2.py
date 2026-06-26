# -*- coding: utf-8 -*-
"""
台股「月均量爆量起漲」每日選股程式  v2（免費官方資料版）
================================================================
與 v1 的差異：資料來源改為「免費、官方」，不再需要 FinMind 付費。
  ● 每日當日全市場資料 = 證交所 + 櫃買官方 OpenAPI（免費、免 token、各 1 次請求）
        上市：https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
        上櫃：https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes
        （兩者都只提供「最新一天」，自帶民國日期，數字乾淨無逗號）
  ● 歷史回補（只做一次）= FinMind 免費版「逐檔」查詢
        官方 OpenAPI 不提供歷史，故首次用 FinMind 免費的逐檔 API 補齊每檔歷史。
        逐檔查詢不是付費鎖的「一次全市場」功能，免費帳號可用，只是有每小時上限。

選股邏輯（與 v1 相同，已驗證）：
  硬性條件：① 爆量(今日量≥N倍月均量20日) ② 站上月線 ③ 價漲量增(收紅)
            ④ 流動性(月均量、成交額、股價門檻) ⑤ 位階不過高(季線乖離上限)
  加分排序：爆量強度、量能持續、突破季高、月線翻揚、站上季線、季線翻揚、多頭排列、站上年線

使用方式：
  1) pip install requests pandas numpy openpyxl     （不需要 finmind 套件）
  2) 到 https://finmindtrade.com 免費註冊取得 token（僅供「一次性歷史回補」用）
  3) 設環境變數 FINMIND_TOKEN，或填到下方 CONFIG
  4) 首次執行：自動抓當日 + 用 FinMind 逐檔回補歷史
        python tw_volume_breakout_screener_v2.py
     首次回補約 1500~1600 檔普通股，受 FinMind 每小時 600 次限制，
     約需 2.5~3.5 小時，可掛著跑；中斷後重跑會自動接續（已補的會跳過）。
  5) 之後每天執行：只用官方 OpenAPI 抓當日（2 次請求、數秒完成）+ 選股。

免責：僅供技術研究與教育用途，不構成投資建議。
"""

import os
import re
import json
import time
import sqlite3
import argparse
import datetime as dt

import requests
import urllib3
import numpy as np
import pandas as pd

# 新版 Python(3.13+)的 OpenSSL 對憑證檢查很嚴格，部分政府網站(如櫃買 tpex.org.tw)
# 的憑證缺少 Subject Key Identifier 欄位而被拒。對「公開、唯讀」的政府開放資料端點
# 關閉憑證驗證是安全且常見的作法，這裡先關閉相關警告訊息。
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
#  CONFIG
# ============================================================
CONFIG = {
    "FINMIND_TOKEN": os.environ.get("FINMIND_TOKEN", ""),  # 僅供首次歷史回補
    "DB_PATH": "twstock.db",
    "OUTPUT_DIR": "output",
    "BACKFILL_DAYS": 400,        # 首次回補日曆天數（~270 交易日，足夠季線、接近年線）
    "BACKFILL_MIN_ROWS": 60,     # 個股 DB 內少於此天數就觸發回補（普通是首次）
    "FINMIND_SLEEP": 6.0,        # 回補時每檔間隔(秒)，配合 600 次/hr 上限（6 秒=600/hr）
    "FRESH_DAYS": 7,             # 選股時：個股最新一筆超過幾天前就視為停牌/已下市，排除
    "HTTP_TIMEOUT": 30,
}

PARAMS = {
    "VOL_MULT":      2.0,    # 爆量：今日量 ≥ N 倍月均量(20日)
    "MIN_PRICE":     10.0,   # 最低股價(元)
    "MIN_VOL_LOTS":  500.0,  # 月均量下限(張)
    "MIN_AMOUNT_E":  0.5,    # 今日成交額下限(億元)
    "MAX_BIAS60":    0.30,   # 季線乖離上限(避免追高)
    "INCLUDE_ETF":   False,  # 是否納入 4 位數 00xx 的 ETF
    "TOP_N":         60,
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


# ============================================================
#  小工具：民國日期、數字清洗
# ============================================================
def roc_to_iso(s):
    """民國日期字串 '1150623' → '2026-06-23'。失敗回 None。"""
    s = str(s).strip()
    if len(s) != 7 or not s.isdigit():
        return None
    y = int(s[:3]) + 1911
    return f"{y:04d}-{s[3:5]}-{s[5:7]}"


def to_float(x):
    """把可能含逗號/空白/'--' 的字串轉 float，無法轉則 NaN。"""
    if x is None:
        return np.nan
    s = str(x).replace(",", "").strip()
    if s in ("", "--", "---", "N/A", "X", "x"):
        return np.nan
    try:
        return float(s)
    except ValueError:
        return np.nan


def is_common_stock(sid):
    """普通股：4 位純數字、且不以 0 開頭（排除 ETF 00xx、權證、債券等含英文者）。"""
    if not isinstance(sid, str) or not re.fullmatch(r"\d{4}", sid):
        return False
    if sid.startswith("00"):
        return PARAMS["INCLUDE_ETF"]
    return True


# ============================================================
#  每日當日資料：官方 OpenAPI（免費）
# ============================================================
def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    # 政府開放資料端點憑證在新版 OpenSSL 下會驗證失敗(缺 Subject Key Identifier)，
    # 這些是公開唯讀資料，關閉憑證驗證以確保可連線。(不影響 FinMind 連線，那條仍驗證)
    s.verify = False
    return s


def fetch_twse_daily(sess):
    """上市全市場最新一日。回傳 normalized DataFrame。"""
    r = sess.get(TWSE_URL, timeout=CONFIG["HTTP_TIMEOUT"])
    r.raise_for_status()
    rows = []
    for d in r.json():
        iso = roc_to_iso(d.get("Date"))
        rows.append({
            "stock_id": d.get("Code", "").strip(),
            "date": iso,
            "name": d.get("Name", "").strip(),
            "market": "上市",
            "open": to_float(d.get("OpeningPrice")),
            "high": to_float(d.get("HighestPrice")),
            "low":  to_float(d.get("LowestPrice")),
            "close": to_float(d.get("ClosingPrice")),
            "volume": to_float(d.get("TradeVolume")),     # 股
            "amount": to_float(d.get("TradeValue")),
        })
    return pd.DataFrame(rows)


def fetch_tpex_daily(sess):
    """上櫃全市場最新一日。回傳 normalized DataFrame。"""
    r = sess.get(TPEX_URL, params={"l": "zh-tw"}, timeout=CONFIG["HTTP_TIMEOUT"])
    r.raise_for_status()
    rows = []
    for d in r.json():
        iso = roc_to_iso(d.get("Date"))
        rows.append({
            "stock_id": d.get("SecuritiesCompanyCode", "").strip(),
            "date": iso,
            "name": d.get("CompanyName", "").strip(),
            "market": "上櫃",
            "open": to_float(d.get("Open")),
            "high": to_float(d.get("High")),
            "low":  to_float(d.get("Low")),
            "close": to_float(d.get("Close")),
            "volume": to_float(d.get("TradingShares")),   # 股
            "amount": to_float(d.get("TransactionAmount")),
        })
    return pd.DataFrame(rows)


def get_today_snapshot(sess, retries=3):
    """抓上市+上櫃當日，過濾普通股，回傳 (price_df, info_df)。失敗會自動重試。"""
    last = None
    for k in range(retries):
        try:
            twse = fetch_twse_daily(sess)
            tpex = fetch_tpex_daily(sess)
            snap = pd.concat([twse, tpex], ignore_index=True)
            snap = snap[snap["stock_id"].map(is_common_stock)].copy()
            snap = snap.dropna(subset=["date", "close"])
            snap = snap[snap["close"] > 0]
            if snap.empty:
                raise RuntimeError("官方 API 回傳空資料")
            info = snap[["stock_id", "name", "market"]].drop_duplicates("stock_id")
            price = snap[["stock_id", "date", "open", "high", "low", "close", "volume", "amount"]]
            twse_n = int((twse["stock_id"].map(is_common_stock)).sum())
            tpex_n = int((tpex["stock_id"].map(is_common_stock)).sum())
            print(f"當日快照：上市 {twse_n} 檔 / 上櫃 {tpex_n} 檔（最新日期 {snap['date'].max()}）")
            return price, info
        except Exception as e:
            last = e
            print(f"  抓當日資料第 {k + 1}/{retries} 次失敗：{e}")
            if k < retries - 1:
                time.sleep(10)
    raise last


# ============================================================
#  歷史回補：FinMind 免費「逐檔」（只做一次）
# ============================================================
def finmind_get(dataset, token, max_retry=5, **params):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    q = {"dataset": dataset, **params}
    wait = 30
    for _ in range(max_retry):
        try:
            resp = requests.get(FINMIND_URL, headers=headers, params=q,
                                timeout=CONFIG["HTTP_TIMEOUT"])
        except requests.RequestException as e:
            print(f"    [連線錯誤] {e}，{wait}s 後重試…"); time.sleep(wait); wait = min(wait*2, 600); continue
        if resp.status_code in (402, 429):   # 流量上限
            print(f"    [FinMind 流量上限] 等待 {wait}s（可中斷，稍後重跑會接續）…")
            time.sleep(wait); wait = min(wait*2, 600); continue
        if resp.status_code != 200:
            print(f"    [HTTP {resp.status_code}] {resp.text[:100]}"); time.sleep(wait); wait = min(wait*2, 600); continue
        return pd.DataFrame(resp.json().get("data", []))
    raise RuntimeError(f"FinMind 請求失敗：{dataset} {params}")


def backfill_one(token, stock_id, start, end):
    """用 FinMind 抓單檔歷史，回傳 normalized rows（list of tuples）。"""
    df = finmind_get("TaiwanStockPrice", token, data_id=stock_id,
                     start_date=start, end_date=end)
    if df.empty:
        return []
    out = []
    for _, r in df.iterrows():
        out.append((stock_id, r["date"],
                    to_float(r.get("open")), to_float(r.get("max")),
                    to_float(r.get("min")), to_float(r.get("close")),
                    to_float(r.get("Trading_Volume")), to_float(r.get("Trading_money"))))
    return out


# ============================================================
#  SQLite
# ============================================================
def init_db(path):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS price(
        stock_id TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, amount REAL, PRIMARY KEY(stock_id, date))""")
    con.execute("""CREATE TABLE IF NOT EXISTS stock(
        stock_id TEXT PRIMARY KEY, name TEXT, market TEXT)""")
    con.commit()
    return con


def upsert_price(con, df):
    if df is None or df.empty:
        return
    con.executemany("INSERT OR REPLACE INTO price VALUES (?,?,?,?,?,?,?,?)",
                    df[["stock_id", "date", "open", "high", "low",
                        "close", "volume", "amount"]].itertuples(index=False, name=None))
    con.commit()


def upsert_info(con, info):
    con.executemany("INSERT OR REPLACE INTO stock VALUES (?,?,?)",
                    info[["stock_id", "name", "market"]].itertuples(index=False, name=None))
    con.commit()


def row_counts(con):
    return dict(con.execute("SELECT stock_id, COUNT(*) FROM price GROUP BY stock_id").fetchall())


def run_backfill(con, token, universe, args):
    """對 DB 內歷史不足的個股做一次性回補。"""
    end = dt.date.today()
    start = (end - dt.timedelta(days=CONFIG["BACKFILL_DAYS"])).isoformat()
    end = end.isoformat()
    counts = row_counts(con)
    todo = sorted(s for s in universe if counts.get(s, 0) < CONFIG["BACKFILL_MIN_ROWS"])
    if not todo:
        print("歷史資料已齊備，略過回補。"); return
    if not token:
        print("【警告】未設定 FINMIND_TOKEN，無法回補歷史。請先設定 token 後重跑。"); return

    cap = args.max_backfill if args.max_backfill else len(todo)
    todo = todo[:cap]
    eta_min = len(todo) * CONFIG["FINMIND_SLEEP"] / 60
    print(f"需回補 {len(todo)} 檔歷史（{start} ~ {end}），預估約 {eta_min:.0f} 分鐘。"
          f"\n可掛著跑，中斷後重跑會自動接續…")
    for i, sid in enumerate(todo, 1):
        try:
            rows = backfill_one(token, sid, start, end)
        except RuntimeError as e:
            print(f"  [{i}/{len(todo)}] {sid} 失敗：{e}（稍後重跑接續）"); break
        if rows:
            con.executemany("INSERT OR REPLACE INTO price VALUES (?,?,?,?,?,?,?,?)", rows)
            con.commit()
        if i % 25 == 0 or i == len(todo):
            print(f"  回補進度 [{i}/{len(todo)}]  最新：{sid} 寫入 {len(rows)} 筆")
        time.sleep(CONFIG["FINMIND_SLEEP"])
    print("本輪回補結束。")


def load_history(con, universe):
    df = pd.read_sql("SELECT * FROM price", con)
    df = df[df["stock_id"].isin(universe)].copy()
    df["date"] = pd.to_datetime(df["date"])
    return df


# ============================================================
#  指標（與 v1 相同，已驗證）
# ============================================================
def compute_indicators(df):
    df = df.sort_values(["stock_id", "date"]).reset_index(drop=True)
    df["vol_lots"] = df["volume"] / 1000.0
    df["amount_e"] = df["amount"] / 1e8
    g = df.groupby("stock_id", group_keys=False)
    for w in (5, 20, 60, 120, 240):
        df[f"ma{w}"] = g["close"].transform(lambda s, w=w: s.rolling(w, min_periods=min(w, 20)).mean())
    df["vol_ma20"]   = g["vol_lots"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    df["vol_ma5"]    = g["vol_lots"].transform(lambda s: s.rolling(5,  min_periods=5).mean())
    df["vol_base20"] = g["vol_lots"].transform(lambda s: s.shift(1).rolling(20, min_periods=20).mean())
    df["vol_ratio"]  = (df["vol_lots"] / df["vol_base20"]).replace([np.inf, -np.inf], np.nan)
    df["vol_persist"] = (df["vol_ma5"] / df["vol_ma20"]).replace([np.inf, -np.inf], np.nan)
    df["prev_close"] = g["close"].transform(lambda s: s.shift(1))
    df["chg_pct"] = (df["close"] / df["prev_close"] - 1) * 100
    df["ma20_up"] = (df["ma20"] - g["ma20"].transform(lambda s: s.shift(5))) > 0
    df["ma60_up"] = (df["ma60"] - g["ma60"].transform(lambda s: s.shift(10))) >= 0
    df["hh60"] = g["high"].transform(lambda s: s.shift(1).rolling(60, min_periods=30).max())
    df["break_hh60"] = df["close"] > df["hh60"]
    df["bias60"] = df["close"] / df["ma60"] - 1
    df["bull_align"] = (df["ma5"] > df["ma20"]) & (df["ma20"] > df["ma60"])
    return df


def score_row(r):
    s = 0.0
    s += min(r["vol_ratio"], 5) / 5 * 30
    s += min(max(r["vol_persist"] - 1, 0), 1) * 15
    s += 15 if r["break_hh60"] else 0
    s += 10 if r["ma20_up"] else 0
    s += 10 if r["close"] > r["ma60"] else 0
    s += 8  if r["ma60_up"] else 0
    s += 7  if r["bull_align"] else 0
    s += 5  if (pd.notna(r["ma240"]) and r["close"] > r["ma240"]) else 0
    return round(s, 1)


def make_flags(r):
    f = []
    if r["break_hh60"]: f.append("突破季高")
    if r["ma20_up"]:    f.append("月線翻揚")
    if r["close"] > r["ma60"]: f.append("站上季線")
    if r["ma60_up"]:    f.append("季線翻揚")
    if r["bull_align"]: f.append("多頭排列")
    if pd.notna(r["ma240"]) and r["close"] > r["ma240"]: f.append("站上年線")
    return "·".join(f)


# ============================================================
#  選股（取每檔「最新一筆」，並排除過舊資料）
# ============================================================
def screen(df, params, info_map):
    df = compute_indicators(df)
    newest = df["date"].max()
    last = df.sort_values("date").groupby("stock_id").tail(1).copy()
    # 排除最新一筆過舊者（停牌/下市）
    last = last[last["date"] >= newest - pd.Timedelta(days=CONFIG["FRESH_DAYS"])]

    hard = (
        (last["vol_ratio"] >= params["VOL_MULT"]) &
        (last["close"] > last["ma20"]) &
        (last["chg_pct"] > 0) &
        (last["vol_ma20"] >= params["MIN_VOL_LOTS"]) &
        (last["amount_e"] >= params["MIN_AMOUNT_E"]) &
        (last["close"] >= params["MIN_PRICE"]) &
        (last["bias60"] <= params["MAX_BIAS60"])
    )
    sel = last[hard].copy()
    if sel.empty:
        return sel, newest
    sel["score"] = sel.apply(score_row, axis=1)
    sel["flags"] = sel.apply(make_flags, axis=1)
    sel["名稱"] = sel["stock_id"].map(lambda s: info_map.get(s, ("", ""))[0])
    sel["市場"] = sel["stock_id"].map(lambda s: info_map.get(s, ("", ""))[1])
    sel["資料日"] = sel["date"].dt.strftime("%Y-%m-%d")
    return sel.sort_values("score", ascending=False).reset_index(drop=True), newest


def output(sel, newest):
    os.makedirs(CONFIG["OUTPUT_DIR"], exist_ok=True)
    dstr = pd.to_datetime(newest).strftime("%Y%m%d")
    cols = ["stock_id", "名稱", "市場", "資料日", "close", "chg_pct", "vol_lots",
            "vol_ma20", "vol_ratio", "vol_persist", "bias60", "score", "flags"]
    rename = {"stock_id": "代號", "close": "收盤", "chg_pct": "漲跌%", "vol_lots": "成交量(張)",
              "vol_ma20": "月均量(張)", "vol_ratio": "量比", "vol_persist": "5日量/月量",
              "bias60": "季線乖離%", "score": "評分", "flags": "強度標記"}
    out = sel[cols].rename(columns=rename)
    out["收盤"] = out["收盤"].round(2); out["量比"] = out["量比"].round(2)
    out["5日量/月量"] = out["5日量/月量"].round(2); out["漲跌%"] = out["漲跌%"].round(2)
    out["季線乖離%"] = (out["季線乖離%"] * 100).round(1)
    out["成交量(張)"] = out["成交量(張)"].round(0).astype("Int64")
    out["月均量(張)"] = out["月均量(張)"].round(0).astype("Int64")

    csv_path = os.path.join(CONFIG["OUTPUT_DIR"], f"breakout_{dstr}.csv")
    out.to_csv(csv_path, index=False, encoding="utf-8-sig")
    try:
        xlsx = os.path.join(CONFIG["OUTPUT_DIR"], f"breakout_{dstr}.xlsx")
        out.to_excel(xlsx, index=False); saved = f"{csv_path}\n  {xlsx}"
    except Exception:
        saved = csv_path

    print("\n" + "=" * 80)
    print(f" {pd.to_datetime(newest).date()}  月均量爆量起漲選股  共 {len(out)} 檔")
    print("=" * 80)
    print(out.head(PARAMS["TOP_N"]).to_string(index=False))
    print(f"\n已輸出：\n  {saved}")
    print("\n提醒：機械式初篩，進場前仍需看籌碼(三大法人/主力)、消息面與基本面。")


# ============================================================
#  投信買賣超（籌碼）：上市 = 證交所 T86（全市場，每日一次）
# ============================================================
T86_URL = "https://www.twse.com.tw/fund/T86"
TRUST_LOOKBACK = 30      # 觀察最近幾個交易日
TRUST_BASE_THR = 50      # 候選基準門檻(張)，網頁端可往上切換到 100/200/500/1000
TRUST_MIN_STREAK = 3     # 連續買超天數門檻


def fetch_twse_t86(sess, ymd):
    """抓某日(YYYYMMDD)上市三大法人買賣超，回傳 [(stock_id, date_iso, 投信淨買張), ...]。"""
    url = f"{T86_URL}?response=json&date={ymd}&selectType=ALL"
    r = sess.get(url, timeout=CONFIG["HTTP_TIMEOUT"])
    r.raise_for_status()
    j = r.json()
    if "tables" in j and j["tables"]:
        tbl = j["tables"][0]
        fields, data = tbl.get("fields", []), tbl.get("data", [])
    else:
        fields, data = j.get("fields", []), j.get("data", [])
    if not fields or not data:
        return []
    try:
        ic = fields.index("證券代號")
        it = fields.index("投信買賣超股數")
    except ValueError:
        return []
    iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    out = []
    for row in data:
        code = str(row[ic]).strip()
        if not is_common_stock(code):
            continue
        net = to_float(row[it])
        out.append((code, iso, round((0.0 if net != net else net) / 1000.0, 1)))
    return out


def update_inst(con, sess):
    """把最近 TRUST_LOOKBACK 個交易日、尚未抓過的『投信買賣超』補進 inst 表（每日通常只差1天）。"""
    con.execute("CREATE TABLE IF NOT EXISTS inst("
                "stock_id TEXT, date TEXT, trust_lots REAL, PRIMARY KEY(stock_id,date))")
    con.commit()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM price ORDER BY date DESC LIMIT ?", (TRUST_LOOKBACK,))]
    have = set(r[0] for r in con.execute("SELECT DISTINCT date FROM inst"))
    todo = [d for d in dates if d not in have]
    if not todo:
        print("投信買賣超：已是最新。")
        return
    print(f"更新投信買賣超：需抓 {len(todo)} 個交易日…")
    n = 0
    for d in sorted(todo):
        try:
            rows = fetch_twse_t86(sess, d.replace("-", ""))
        except Exception as e:
            print(f"  T86 {d} 失敗：{e}")
            continue
        if rows:
            con.executemany("INSERT OR IGNORE INTO inst VALUES (?,?,?)", rows)
            con.commit()
            n += 1
        time.sleep(1.2)
    print(f"投信買賣超更新完成：新增 {n} 個交易日。")


def build_trust_candidates(con):
    """挑出『最近一個月內、投信曾連續≥3日淨買≥基準門檻』的個股，
    並附上其近一個月每日 [日期, 投信淨買張, 收盤, 最高, 成交張]，供網頁端依門檻即時運算與排序。"""
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM inst ORDER BY date DESC LIMIT ?", (TRUST_LOOKBACK,))]
    if not dates:
        return {}
    dmin = min(dates)
    rows = con.execute(
        "SELECT i.stock_id, i.date, i.trust_lots, p.close, p.high, p.volume "
        "FROM inst i JOIN price p ON p.stock_id=i.stock_id AND p.date=i.date "
        "WHERE i.date >= ? ORDER BY i.stock_id, i.date", (dmin,)).fetchall()
    from collections import defaultdict
    by = defaultdict(list)
    for sid, d, t, c, h, v in rows:
        if c is None or h is None:
            continue
        by[sid].append([d, round(t or 0, 1), round(c, 2), round(h, 2), round((v or 0) / 1000.0, 1)])
    info = {r[0]: (r[1], r[2]) for r in con.execute("SELECT stock_id,name,market FROM stock")}
    out = {}
    for sid, series in by.items():
        if len(series) < TRUST_MIN_STREAK:
            continue
        best = run = 0
        for s in series:
            if s[1] >= TRUST_BASE_THR:
                run += 1
                best = max(best, run)
            else:
                run = 0
        if best >= TRUST_MIN_STREAK:
            nm, mk = info.get(sid, ("", ""))
            out[sid] = {"name": nm, "market": mk, "series": series}
    return out


def output_trust(cands):
    """輸出 output/trust_YYYYMMDD.json（給看板第三分頁讀取）。"""
    os.makedirs(CONFIG["OUTPUT_DIR"], exist_ok=True)
    last = "00000000"
    for v in cands.values():
        if v["series"]:
            last = max(last, v["series"][-1][0].replace("-", ""))
    if last == "00000000":
        last = dt.date.today().strftime("%Y%m%d")
    iso = f"{last[:4]}-{last[4:6]}-{last[6:8]}"
    path = os.path.join(CONFIG["OUTPUT_DIR"], f"trust_{last}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": iso, "base_thr": TRUST_BASE_THR, "min_streak": TRUST_MIN_STREAK,
                   "data": cands}, f, ensure_ascii=False)
    print(f"已輸出投信連買候選：{path}")


def write_empty_csv(newest=None):
    """無入選或抓不到當日資料時，仍輸出一份只有表頭的 CSV，
    確保看板程式一定找得到檔案，整條流程不會中斷。"""
    os.makedirs(CONFIG["OUTPUT_DIR"], exist_ok=True)
    dstr = (pd.to_datetime(newest).strftime("%Y%m%d") if newest is not None
            else dt.date.today().strftime("%Y%m%d"))
    cols = ["代號", "名稱", "市場", "資料日", "收盤", "漲跌%", "成交量(張)", "月均量(張)",
            "量比", "5日量/月量", "季線乖離%", "評分", "強度標記"]
    path = os.path.join(CONFIG["OUTPUT_DIR"], f"breakout_{dstr}.csv")
    pd.DataFrame(columns=cols).to_csv(path, index=False, encoding="utf-8-sig")
    print(f"已輸出空清單：{path}")


# ============================================================
#  主程式
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-backfill", type=int, default=0,
                    help="本次最多回補幾檔(0=全部)；想分批跑可設小一點")
    ap.add_argument("--skip-update", action="store_true",
                    help="略過抓當日(僅用現有 DB 選股，除錯用)")
    args = ap.parse_args()

    con = init_db(CONFIG["DB_PATH"])
    sess = _session()

    # 1) 抓當日全市場（官方免費）→ 寫入 DB。抓失敗不中止，改用資料庫既有資料選股。
    snap_ids = set()
    if not args.skip_update:
        try:
            price, info = get_today_snapshot(sess)
            upsert_price(con, price)
            upsert_info(con, info)
            snap_ids = set(info["stock_id"])
        except Exception as e:
            print(f"抓當日資料失敗：{e}\n→ 改用資料庫既有最新資料選股（不影響整體流程）。")

    # 標的清單：資料庫 ∪ 當日快照（首次執行資料庫為空，靠快照）
    info_all = pd.read_sql("SELECT * FROM stock", con)
    universe = set(info_all["stock_id"]) | snap_ids
    if not universe:
        print("無可用標的（首次執行卻抓不到當日資料）。請稍後重新執行。")
        write_empty_csv(); con.close(); return
    info_map = {r.stock_id: (r.name, r.market) for r in info_all.itertuples()}

    # 2) 一次性歷史回補（FinMind 免費逐檔）
    run_backfill(con, CONFIG["FINMIND_TOKEN"], universe, args)

    # 3) 載入歷史、計算、選股、輸出（無論有無入選都輸出 CSV）
    hist = load_history(con, universe)
    if hist.empty:
        print("DB 無歷史資料，請先完成回補。"); write_empty_csv(); con.close(); return

    sel, newest = screen(hist, PARAMS, info_map)
    if sel.empty:
        print(f"\n{pd.to_datetime(newest).date()} 無符合條件之標的，輸出空清單。")
        write_empty_csv(newest)
    else:
        output(sel, newest)

    # 投信連續買超（額外輸出 trust_*.json；失敗不影響上面的爆量清單）
    try:
        update_inst(con, sess)
        cands = build_trust_candidates(con)
        output_trust(cands)
        print(f"投信連買候選：{len(cands)} 檔（張數門檻於網頁端切換）")
    except Exception as e:
        print(f"投信資料/篩選失敗（不影響爆量清單）：{e}")

    con.close()


if __name__ == "__main__":
    main()
