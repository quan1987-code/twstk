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
    "BACKFILL_DAYS": 400,        # （備用）回補日曆天數
    "BACKFILL_START": "2005-01-01",  # 個股歷史深度回補起始日（補到 2005 年初）
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


def _finmind_oneday(token, d):
    """FinMind TaiwanStockPrice 單日(start=end=d)取全市場，回傳 normalized df 或 None。
    單日查詢比『不帶 data_id 抓區間』可靠（後者常被截斷成舊資料）。"""
    try:
        df = finmind_get("TaiwanStockPrice", token, start_date=d, end_date=d)
    except Exception as e:
        print(f"    FinMind {d} 失敗：{e}")
        return None
    if df is None or df.empty:
        return None
    df = df.rename(columns={"max": "high", "min": "low",
                            "Trading_Volume": "volume", "Trading_money": "amount"})
    need = ["stock_id", "date", "open", "high", "low", "close", "volume", "amount"]
    for c in need:
        if c not in df.columns:
            df[c] = None
    df = df[df["stock_id"].astype(str).map(is_common_stock)].copy()
    for c in ("open", "high", "low", "close", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df = df[df["close"] > 0]
    return df[need] if not df.empty else None


def fetch_finmind_prices_recent(token, want_days=4, scan=12):
    """個股日線『以 FinMind 為主』：逐交易日抓最近 want_days 個交易日的全市場日線。
    從今天往回掃最多 scan 個日曆日(跳週末/休市)，蒐集到足夠交易日即停。
    回傳合併後的 normalized df；全失敗回 None（呼叫端沿用官方 OpenAPI 快照）。"""
    if not token:
        return None
    frames = []
    got = 0
    d = dt.date.today()
    for _ in range(scan):
        if got >= want_days:
            break
        if d.weekday() < 5:                       # 跳週末（國定假日靠『回空就跳過』處理）
            one = _finmind_oneday(token, d.isoformat())
            if one is not None and not one.empty:
                frames.append(one)
                got += 1
        d -= dt.timedelta(days=1)
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


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
    """對尚未『深度回補到起始日』的個股做一次性回補（補到 2005）。
    用 deep_done 表記錄已回補的個股，避免每天重抓；新上市股之後也只會補一次。"""
    con.execute("CREATE TABLE IF NOT EXISTS deep_done(stock_id TEXT PRIMARY KEY)")
    con.commit()
    end = dt.date.today().isoformat()
    start = CONFIG.get("BACKFILL_START") or (dt.date.today() - dt.timedelta(days=CONFIG["BACKFILL_DAYS"])).isoformat()
    done = set(r[0] for r in con.execute("SELECT stock_id FROM deep_done"))
    todo = sorted(s for s in universe if s not in done)
    if not todo:
        print(f"個股歷史已深度回補（至 {start}），略過回補。"); return
    if not token:
        print("【警告】未設定 FINMIND_TOKEN，無法回補歷史。請先設定 token 後重跑。"); return

    cap = args.max_backfill if args.max_backfill else len(todo)
    todo = todo[:cap]
    eta_min = len(todo) * CONFIG["FINMIND_SLEEP"] / 60
    print(f"需深度回補 {len(todo)} 檔歷史（{start} ~ {end}），預估約 {eta_min:.0f} 分鐘"
          f"（約 {eta_min/60:.1f} 小時）。\n首次很久、但只做一次；中斷後重跑會自動接續…")
    for i, sid in enumerate(todo, 1):
        try:
            rows = backfill_one(token, sid, start, end)
        except RuntimeError as e:
            print(f"  [{i}/{len(todo)}] {sid} 失敗：{e}（稍後重跑接續）"); break
        if rows:
            con.executemany("INSERT OR REPLACE INTO price VALUES (?,?,?,?,?,?,?,?)", rows)
        con.execute("INSERT OR IGNORE INTO deep_done VALUES (?)", (sid,))   # 標記此檔已回補
        con.commit()
        if i % 25 == 0 or i == len(todo):
            print(f"  回補進度 [{i}/{len(todo)}]  最新：{sid} 寫入 {len(rows)} 筆")
        time.sleep(CONFIG["FINMIND_SLEEP"])
    left = len(set(s for s in universe if s not in set(r[0] for r in con.execute('SELECT stock_id FROM deep_done'))))
    print(f"本輪回補結束。尚餘 {left} 檔待回補（下次執行續抓）。" if left else "本輪回補結束。全部個股已深度回補完成。")


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
# ⑦ 法人動向：三大法人金額(BFI82U) / 融資融券(MI_MARGN) / 外資台指期(TAIFEX OpenAPI)
BFI82U_URL = "https://www.twse.com.tw/fund/BFI82U"
MI_MARGN_URL = "https://www.twse.com.tw/exchangeReport/MI_MARGN"
TAIFEX_FUT_URL = ("https://openapi.taifex.com.tw/v1/"
                  "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate")
TAIFEX_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
TRUST_LOOKBACK = 30      # 連買候選觀察窗（最近約一個月）
INST_LOOKBACK = 250      # 每日 T86 前向更新的交易日數（近端；更深的歷史另由 FinMind 回補，見下）
TRUST_BASE_THR = 50      # 候選基準門檻(張)，網頁端可往上切換到 100/200/500/1000
TRUST_MIN_STREAK = 3     # 連續買超天數門檻

# 個股K線副圖「深度歷史」目標起始日。以「快取 DB＋每次 run 上限」分批補齊，不影響每日主流程；
# 上限可用環境變數調高做一次性長跑（見 daily.yml 的 deep_backfill）。
# 主力(三大法人)＝逐檔抓(data_id)避免全市場單次列數上限截斷漏股，涵蓋上市＋上櫃；400大戶＝全市場週抓。
HISTORY_START = os.environ.get("HISTORY_START", "2019-01-01") or "2019-01-01"           # 400張大戶回補起點
INST_HISTORY_START = os.environ.get("INST_HISTORY_START", "2020-01-01") or "2020-01-01"  # 主力(三大法人)回補起點
INST_HIST_DATASET = "TaiwanStockInstitutionalInvestorsBuySell"  # 三大法人個股表（含 2005 以來）
INST_BACKFILL_PER_RUN = int(os.environ.get("INST_BACKFILL_PER_RUN", "150") or "150")  # 每次 run 逐檔回補的『個股數』上限
SHAREHOLD_MAX_PER_RUN = int(os.environ.get("SHAREHOLD_MAX_PER_RUN", "12") or "12")     # 每次 run 回補週數上限
# 上櫃(OTC)主力近端更新：證交所 T86 只含上市，上櫃改用 FinMind 逐檔補近端，讓上櫃個股主力每天更新。
OTC_INST_LOOKBACK = int(os.environ.get("OTC_INST_LOOKBACK", "60") or "60")            # 從未有主力資料之上櫃股，近端先補的交易日窗（深史仍由 backfill 補）
OTC_INST_MAX_PER_RUN = int(os.environ.get("OTC_INST_MAX_PER_RUN", "2000") or "2000")  # 每次 run 逐檔補的上櫃股數上限（保護 API 額度/時間）


def _t86_indices(fields):
    def find(*cands):
        for c in cands:
            if c in fields:
                return fields.index(c)
        for i, f in enumerate(fields):
            if any(c in f for c in cands):
                return i
        return None
    return {
        "code": find("證券代號"),
        "fmain": find("外陸資買賣超股數(不含外資自營商)", "外資及陸資買賣超股數(不含外資自營商)"),
        "fdeal": find("外資自營商買賣超股數"),
        "trust": find("投信買賣超股數"),
        "dealer": find("自營商買賣超股數"),
        "total": find("三大法人買賣超股數"),
    }


def fetch_twse_t86(sess, ymd):
    """抓某日(YYYYMMDD)上市三大法人買賣超，回傳
    [(stock_id, date_iso, 外資張, 投信張, 自營張, 三大法人合計張), ...]。缺欄位以 None 表示。"""
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
    idx = _t86_indices(fields)
    if idx["code"] is None or idx["trust"] is None:
        return []
    iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"

    def lots(row, i):
        if i is None or i >= len(row):
            return None
        v = to_float(row[i])
        return None if v != v else round(v / 1000.0, 1)   # 股數 → 張

    out = []
    for row in data:
        code = str(row[idx["code"]]).strip()
        if not is_common_stock(code):
            continue
        fm, fd = lots(row, idx["fmain"]), lots(row, idx["fdeal"])
        foreign = None if (fm is None and fd is None) else round((fm or 0) + (fd or 0), 1)
        trust = lots(row, idx["trust"])
        dealer = lots(row, idx["dealer"])
        total = lots(row, idx["total"])
        out.append((code, iso, foreign, trust, dealer, total))
    return out


def update_inst(con, sess):
    """把最近 INST_LOOKBACK 個交易日的『三大法人(外資/投信/自營/合計)買賣超』補進 inst 表。
    舊版只有 trust_lots，本版新增 foreign/dealer/total 三欄；缺這些欄位(total IS NULL)的日期會重抓一次。"""
    con.execute("CREATE TABLE IF NOT EXISTS inst("
                "stock_id TEXT, date TEXT, trust_lots REAL, PRIMARY KEY(stock_id,date))")
    for col in ("foreign_lots", "dealer_lots", "total_lots"):
        try:
            con.execute(f"ALTER TABLE inst ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass   # 欄位已存在
    con.commit()
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM price ORDER BY date DESC LIMIT ?", (INST_LOOKBACK,))]
    have = set(r[0] for r in con.execute(
        "SELECT DISTINCT date FROM inst WHERE total_lots IS NOT NULL"))
    todo = [d for d in dates if d not in have]
    if not todo:
        print("三大法人買賣超：已是最新。")
        return
    print(f"更新三大法人買賣超：需抓 {len(todo)} 個交易日（首次含補三大法人欄位）…")
    n = 0
    for d in sorted(todo):
        try:
            rows = fetch_twse_t86(sess, d.replace("-", ""))
        except Exception as e:
            print(f"  T86 {d} 失敗：{e}")
            continue
        if rows:
            con.executemany(
                "INSERT OR REPLACE INTO inst"
                "(stock_id,date,foreign_lots,trust_lots,dealer_lots,total_lots)"
                " VALUES (?,?,?,?,?,?)", rows)
            con.commit()
            n += 1
        time.sleep(1.2)
    print(f"三大法人買賣超更新完成：新增/補齊 {n} 個交易日。")


_INST_CAT_CACHE = {}


def _inst_cat(name):
    """FinMind 三大法人類別 name → 'foreign'/'trust'/'dealer'/None。
    相容英文 token（Foreign_Investor / Foreign_Dealer_Self / Investment_Trust / Dealer*）
    與中文（外資 / 外陸資 / 外資自營商 / 投信 / 自營商…）。
    順序：外資優先，讓『外資自營商』歸入 foreign（與 T86 的 foreign 含外資自營商一致）。"""
    if name in _INST_CAT_CACHE:
        return _INST_CAT_CACHE[name]
    s = str(name)
    low = s.lower()
    if "foreign" in low or "外" in s:        # 外資 + 外資自營商
        cat = "foreign"
    elif "trust" in low or "投信" in s:
        cat = "trust"
    elif "dealer" in low or "自營" in s:      # 自營商（自行 + 避險）
        cat = "dealer"
    else:
        cat = None
    _INST_CAT_CACHE[name] = cat
    return cat


def _inst_rows_from_finmind(sid, df):
    """把 FinMind『TaiwanStockInstitutionalInvestorsBuySell』單檔 DataFrame 轉成 inst 表 rows：
    [(stock_id, date, 外資張, 投信張, 自營張, 三大法人合計張), ...]。
    全零日留白（不寫；網頁端前向填 0，省空間）。回補與上櫃近端更新共用，確保解析一致。"""
    if df is None or df.empty or not {"date", "name", "buy", "sell"} <= set(df.columns):
        return []
    df = df.copy()
    df["_net"] = (pd.to_numeric(df["buy"], errors="coerce").fillna(0)
                  - pd.to_numeric(df["sell"], errors="coerce").fillna(0))
    df["_cat"] = df["name"].map(_inst_cat)
    rows = []
    for d, g in df.groupby("date"):
        fnet = float(g.loc[g["_cat"] == "foreign", "_net"].sum())
        tnet = float(g.loc[g["_cat"] == "trust", "_net"].sum())
        dnet = float(g.loc[g["_cat"] == "dealer", "_net"].sum())
        if fnet == 0 and tnet == 0 and dnet == 0:
            continue   # 全零：留白，網頁端前向填 0
        tot = fnet + tnet + dnet
        rows.append((sid, str(d), round(fnet / 1000.0, 1), round(tnet / 1000.0, 1),
                     round(dnet / 1000.0, 1), round(tot / 1000.0, 1)))
    return rows


def _next_day_iso(iso):
    """ISO 日字串 → 隔一日曆日 ISO（FinMind start_date 為含界、以日曆日計，週末自然無資料）。"""
    return (dt.date.fromisoformat(iso) + dt.timedelta(days=1)).isoformat()


def backfill_inst_history(con, token):
    """把『三大法人(外資/投信/自營/合計)買賣超』逐檔回補到 INST_HISTORY_START，涵蓋上市＋上櫃。
    ★改為『逐檔』抓取(data_id=sid)：先前用全市場單日抓取會被 FinMind 單次回傳列數上限截斷，
    導致很多個股（尤其非權值、及全部上櫃）只剩近端 T86 的資料、歷史整段缺。逐檔抓可保證每檔完整。
    用 inst_done 表記錄已處理個股避免重抓；只挑『歷史不足或從未抓過』者，每次 run 上限
    INST_BACKFILL_PER_RUN 檔（deep_backfill 時調高一次補完）。近端上市仍由 update_inst(T86) 每日更新。"""
    if not token:
        print("主力歷史回補：無 FinMind token，略過。")
        return
    con.execute("CREATE TABLE IF NOT EXISTS inst("
                "stock_id TEXT, date TEXT, trust_lots REAL, PRIMARY KEY(stock_id,date))")
    for col in ("foreign_lots", "dealer_lots", "total_lots"):
        try:
            con.execute(f"ALTER TABLE inst ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass
    con.execute("CREATE TABLE IF NOT EXISTS inst_done(stock_id TEXT PRIMARY KEY, done_date TEXT)")
    con.commit()
    latest = con.execute("SELECT MAX(date) FROM price").fetchone()[0]
    if not latest:
        return
    # 已足夠(min(total_lots 日期) 早於 cutoff)或已處理過(inst_done)的個股跳過，避免重抓。
    cutoff = (dt.date.fromisoformat(INST_HISTORY_START) + dt.timedelta(days=45)).isoformat()
    mind = {r[0]: r[1] for r in con.execute(
        "SELECT stock_id, MIN(date) FROM inst WHERE total_lots IS NOT NULL GROUP BY stock_id")}
    done = set(r[0] for r in con.execute("SELECT stock_id FROM inst_done"))
    universe = sorted(r[0] for r in con.execute("SELECT DISTINCT stock_id FROM price"))
    todo = [s for s in universe
            if is_common_stock(s) and s not in done and (s not in mind or mind[s] > cutoff)]
    if not todo:
        print(f"主力歷史逐檔回補：所有個股已補齊至 {INST_HISTORY_START}。")
        return
    todo_total = len(todo)
    todo = todo[:INST_BACKFILL_PER_RUN]
    print(f"主力歷史逐檔回補(上市+上櫃)：待補 {todo_total} 檔，本次 {len(todo)} 檔"
          f"（目標 {INST_HISTORY_START}；其餘後續 run 逐步補齊）…")
    n = 0
    for sid in todo:
        try:
            df = finmind_get(INST_HIST_DATASET, token, max_retry=2,
                             data_id=sid, start_date=INST_HISTORY_START, end_date=latest)
        except Exception as e:
            print(f"  三大法人 {sid} 失敗：{e}")
            continue   # 未標記 done → 下次 run 再試
        rows = _inst_rows_from_finmind(sid, df)
        if rows:
            con.executemany(
                "INSERT OR REPLACE INTO inst"
                "(stock_id,date,foreign_lots,trust_lots,dealer_lots,total_lots)"
                " VALUES (?,?,?,?,?,?)", rows)
        # 不論抓到與否都標記 done（即使該股 FinMind 無資料，也不必每次重試）
        con.execute("INSERT OR REPLACE INTO inst_done(stock_id,done_date) VALUES (?,?)", (sid, latest))
        con.commit()
        n += 1
        time.sleep(0.6)
    print(f"主力歷史逐檔回補完成：本次 {n} 檔（其餘後續 run 逐步補齊，目標 {INST_HISTORY_START}）。")


def update_inst_otc(con, token):
    """上櫃(OTC)股的三大法人買賣超『近端每日更新』。
    證交所 T86（update_inst）只含上市，上櫃個股的近端主力先前只靠一次性 backfill_inst_history
    回補、之後就凍結，導致上櫃個股K線下圖『主力買賣超』近幾天長期空白。本函式改用 FinMind
    逐檔（data_id）補上櫃股，避免全市場單日抓被 FinMind 單次列數上限截斷。
    以 inst_otc_done(through_date) 記錄每檔已補到哪天：每檔只從『現有最新主力日之次日』補到最新
    股價日，故隔天只需抓新增的 1~數日；全零尾巴日也因標記推進而不會反覆重抓。深史仍由 backfill 負責。"""
    if not token:
        print("上櫃主力近端更新：無 FinMind token，略過。")
        return
    con.execute("CREATE TABLE IF NOT EXISTS inst_otc_done("
                "stock_id TEXT PRIMARY KEY, through_date TEXT)")
    con.commit()
    latest = con.execute("SELECT MAX(date) FROM price").fetchone()[0]
    if not latest:
        return
    # 從未有主力資料之上櫃股，近端起補的下限日（深史交給 backfill；避免此處抓過長區間）
    floor_rows = con.execute(
        "SELECT DISTINCT date FROM price ORDER BY date DESC LIMIT ?", (OTC_INST_LOOKBACK,)).fetchall()
    floor = floor_rows[-1][0] if floor_rows else latest
    otc = [r[0] for r in con.execute(
        "SELECT DISTINCT p.stock_id FROM price p JOIN stock s ON s.stock_id=p.stock_id "
        "WHERE s.market='上櫃'")]
    otc = [s for s in otc if is_common_stock(s)]
    if not otc:
        print("上櫃主力近端更新：DB 內無上櫃普通股（可能市場別未標記），略過。")
        return
    # 每檔現有最新主力日、與上次已補到的日期
    maxd = {r[0]: r[1] for r in con.execute(
        "SELECT stock_id, MAX(date) FROM inst WHERE total_lots IS NOT NULL GROUP BY stock_id")}
    through = {r[0]: r[1] for r in con.execute("SELECT stock_id,through_date FROM inst_otc_done")}

    def start_of(sid):
        base = through.get(sid) or maxd.get(sid)   # 上次補到 / 現有最新主力日
        return _next_day_iso(base) if base else floor  # 從未有資料：只補近端窗

    todo = [s for s in otc if start_of(s) <= latest]
    if not todo:
        print("上櫃主力近端更新：已是最新。")
        return
    todo_total = len(todo)
    # 最落後者優先（未補過 / through_date 最舊）：萬一設了上限，先補最該補的
    todo.sort(key=lambda s: through.get(s) or "")
    todo = todo[:OTC_INST_MAX_PER_RUN]
    print(f"上櫃主力近端更新：{todo_total} 檔需補，本次 {len(todo)} 檔（FinMind 逐檔補至 {latest}）…")
    n = 0
    for sid in todo:
        start = start_of(sid)
        try:
            df = finmind_get(INST_HIST_DATASET, token, max_retry=3,
                             data_id=sid, start_date=start, end_date=latest)
        except Exception as e:
            print(f"  上櫃主力 {sid} 失敗：{e}")
            continue   # 未推進 through → 下次 run 再試
        rows = _inst_rows_from_finmind(sid, df)
        if rows:
            con.executemany(
                "INSERT OR REPLACE INTO inst"
                "(stock_id,date,foreign_lots,trust_lots,dealer_lots,total_lots)"
                " VALUES (?,?,?,?,?,?)", rows)
            n += 1
        # 不論當區間是否全零都推進 through（避免全零尾巴反覆重抓）
        con.execute("INSERT OR REPLACE INTO inst_otc_done(stock_id,through_date) VALUES (?,?)",
                    (sid, latest))
        con.commit()
        time.sleep(0.6)
    print(f"上櫃主力近端更新完成：本次 {n} 檔有新增/補齊（共處理 {len(todo)} 檔）。")


def _is_big400_level(level):
    """集保股權分散級距是否屬『400張(=400,000股)以上大戶』（下界 ≥ 400,001 股）。
    相容兩種級距標記：範圍字串(如 '400,001-600,000'、'more than 1,000,001'、'1,000,001以上')
    與純數字級距索引(1~15，其中 12~15 為 ≥400,001 股的四個大戶級距)。"""
    s = str(level).strip()
    if re.fullmatch(r"\d{1,2}", s):          # 純級距索引
        return int(s) in (12, 13, 14, 15)
    low = s.lower()
    if "total" in low or "合計" in s or "差異" in s:
        return False
    m = re.search(r"([\d,]+)", s)
    if not m:
        return False
    try:
        return int(m.group(1).replace(",", "")) >= 400001
    except ValueError:
        return False


def update_shareholding(con, token):
    """集保股權分散：計算『400張以上大戶持股比率(%)』週資料，供個股K線副圖。
    FinMind TaiwanStockHoldingSharesPer 依日期回傳全市場；回補到 HISTORY_START，
    以『最近週優先、每次 run 上限 SHAREHOLD_MAX_PER_RUN 週』分批補齊（DB 有快取，不重抓）。
    包在 try/except 內呼叫，失敗不影響主流程；資料週更新，多數日子只需 1 次探測即『已是最新』。"""
    con.execute("CREATE TABLE IF NOT EXISTS shareholding("
                "stock_id TEXT, date TEXT, big400_pct REAL, PRIMARY KEY(stock_id,date))")
    con.commit()
    if not token:
        print("集保大戶：無 FinMind token，略過。")
        return
    # 用 2330 探測「可用週日期清單」（1 call）；回補目標一路到 HISTORY_START
    try:
        probe = finmind_get("TaiwanStockHoldingSharesPer", token, max_retry=3,
                            data_id="2330", start_date=HISTORY_START)
    except Exception as e:
        print(f"集保大戶：探測失敗，略過（{e}）。")
        return
    if probe is None or probe.empty or "date" not in probe.columns:
        print("集保大戶：探測無資料，略過。")
        return
    all_dates = sorted(str(x) for x in probe["date"].unique())
    want = [d for d in all_dates if d >= HISTORY_START]
    have = set(r[0] for r in con.execute("SELECT DISTINCT date FROM shareholding"))
    todo = [d for d in want if d not in have]
    if not todo:
        print("集保大戶：已是最新。")
        return
    todo_total = len(todo)
    todo = todo[-SHAREHOLD_MAX_PER_RUN:]   # 最近週優先；其餘留待後續每日 run 補回（避免單次長跑）
    if todo_total > len(todo):
        print(f"集保大戶：待補 {todo_total} 週，本次先抓最近 {len(todo)} 週（其餘後續 run 逐步補齊）…")
    else:
        print(f"集保大戶：需抓 {len(todo)} 週（每週全市場一次）…")
    n = 0
    logged_levels = False
    for d in todo:
        try:
            # 注意：本 dataset 需要 start_date（用 date= 會被 FinMind 回 400「start_date missing」）；
            # 以 start_date=end_date=d 取「該週全市場」快照。max_retry=2 讓失敗週約 30s 內放棄、下次補。
            df = finmind_get("TaiwanStockHoldingSharesPer", token, max_retry=2,
                             start_date=d, end_date=d)
        except Exception as e:
            print(f"  集保 {d} 失敗：{e}")
            continue
        if df is None or df.empty or "HoldingSharesLevel" not in df.columns or "percent" not in df.columns:
            continue
        if not logged_levels:   # 首批印出實際級距標記，方便日後於 CI log 核對大戶判定
            print(f"  集保級距樣本：{sorted(df['HoldingSharesLevel'].astype(str).unique())}")
            logged_levels = True
        big = df[df["HoldingSharesLevel"].map(_is_big400_level)].copy()
        big["_sid"] = big["stock_id"].astype(str)
        big["_pct"] = pd.to_numeric(big["percent"], errors="coerce")
        rows = []
        for sid, g in big.groupby("_sid"):
            if len(sid) != 4 or not sid.isdigit():
                continue
            pct = round(float(g["_pct"].sum()), 2)
            if pct == pct and pct > 0:   # 排除 NaN / 0
                rows.append((sid, d, pct))
        if rows:
            con.executemany(
                "INSERT OR REPLACE INTO shareholding(stock_id,date,big400_pct) VALUES (?,?,?)", rows)
            con.commit()
            n += 1
        time.sleep(1.0)
    if want:   # 只保留 HISTORY_START 之後（更舊的清掉）
        con.execute("DELETE FROM shareholding WHERE date < ?", (HISTORY_START,))
        con.commit()
    print(f"集保大戶更新完成：新增/補齊 {n} 週（目標 {HISTORY_START}）。")


def update_issued_shares(con, token):
    """發行張數（表頭『發行 / 流通張數』）。
    來源＝『集保股權分散 TaiwanStockHoldingSharesPer』各級距股數：取 'total'(合計) 列的 unit，
    無 total 列則加總各級距(排除 total/差異)＝集保總股數 ≈ 發行股數。與 400張大戶% 同源同分母，
    流通張數計算最一致；此 dataset 全市場抓取已驗證可用（400大戶即由它產生）。
    只取最新一週、每檔一次。★刻意在深度回補『之前』呼叫，確保 FinMind 額度尚足（回補會大量用量、
    易把後面的請求擠到限流而失敗）。1~2 次請求、失敗不影響主流程；發行數近乎不變，存 DB 快取後長期有效。
    流通張數 = 發行 ×(1−400張大戶%) 於網頁端計算。"""
    con.execute("CREATE TABLE IF NOT EXISTS stockmeta("
                "stock_id TEXT PRIMARY KEY, issued_lots REAL, updated TEXT)")
    con.commit()
    if not token:
        print("發行張數：無 FinMind token，略過。")
        return
    # 探測最新可用週（用 2330，1 call）
    try:
        probe = finmind_get("TaiwanStockHoldingSharesPer", token, max_retry=3,
                            data_id="2330", start_date=HISTORY_START)
    except Exception as e:
        print(f"發行張數：探測失敗，略過（{e}）。")
        return
    if probe is None or probe.empty or "date" not in probe.columns:
        print("發行張數：探測無資料，略過。")
        return
    d = sorted(str(x) for x in probe["date"].unique())[-1]
    # 全市場最新一週（1 call）
    try:
        df = finmind_get("TaiwanStockHoldingSharesPer", token, max_retry=2,
                         start_date=d, end_date=d)
    except Exception as e:
        print(f"發行張數：抓取失敗，略過（{e}）。")
        return
    if (df is None or df.empty or "unit" not in df.columns
            or "stock_id" not in df.columns or "HoldingSharesLevel" not in df.columns):
        print("發行張數：無資料或缺欄位，略過。")
        return
    df = df.copy()
    df["_sid"] = df["stock_id"].astype(str)
    df["_lvl"] = df["HoldingSharesLevel"].astype(str)
    df["_unit"] = pd.to_numeric(df["unit"], errors="coerce").fillna(0)
    rows = []
    for sid, g in df.groupby("_sid"):
        if len(sid) != 4 or not sid.isdigit():   # 普通股 + 00xx ETF；排除權證等
            continue
        low = g["_lvl"].str.lower()
        is_total = low.str.contains("total") | g["_lvl"].str.contains("合計")
        if is_total.any():
            shares = float(g.loc[is_total, "_unit"].max())
        else:   # 無 total 列：加總各級距（排除 total/合計/差異）
            keep = ~(is_total | g["_lvl"].str.contains("差異"))
            shares = float(g.loc[keep, "_unit"].sum())
        if shares > 0:
            rows.append((sid, round(shares / 1000.0), d))
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO stockmeta(stock_id,issued_lots,updated) VALUES (?,?,?)", rows)
        con.commit()
    print(f"發行張數更新完成：{len(rows)} 檔（集保總股數，週 {d}）。")


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


# ============================================================
#  ⑦ 法人動向：三大法人金額 / 融資融券餘額 / 外資台指期淨未平倉
#     （三個全新資料源；抓取失敗都不影響主流程，並印出診斷供雲端 log 驗證）
# ============================================================
def _fmt_date(s):
    s = str(s).strip().replace("/", "").replace("-", "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if (len(s) == 8 and s.isdigit()) else str(s)


def _num2(s):
    try:
        return float(str(s).replace(",", "").replace("%", "").replace("+", "").strip())
    except Exception:
        return None


def fetch_inst3(sess, ymd):
    """三大法人買賣金額 BFI82U（單位：元 → 回傳億元淨額）。"""
    r = sess.get(BFI82U_URL, params={"response": "json", "dayDate": ymd, "type": "day"},
                 timeout=CONFIG["HTTP_TIMEOUT"])
    j = r.json()
    if j.get("stat") != "OK":
        print(f"  [BFI82U] stat={j.get('stat')}（{ymd}）"); return None
    rows = j.get("data") or (j.get("tables", [{}])[0].get("data") if j.get("tables") else [])
    if not rows:
        return None
    foreign = trust = dealer = None
    for row in rows:
        name = str(row[0]); net = _num2(row[-1])  # 買賣差額為最後一欄
        if net is None:
            continue
        if "外資" in name:
            foreign = (foreign or 0) + net          # 外資及陸資 + 外資自營商
        elif "投信" in name:
            trust = (trust or 0) + net
        elif "自營" in name:
            dealer = (dealer or 0) + net            # 自營商(自行買賣)+(避險)
    if foreign is None and trust is None and dealer is None:
        return None
    e = lambda x: round(x / 1e8, 1) if x is not None else None
    total = (foreign or 0) + (trust or 0) + (dealer or 0)
    out = {"date": _fmt_date(j.get("date") or ymd), "foreign": e(foreign),
           "trust": e(trust), "dealer": e(dealer), "total": e(total)}
    print(f"  [BFI82U] {out}")
    return out


def fetch_margin(sess, ymd):
    """全市場融資融券餘額 MI_MARGN（tables[0]=彙總）。融資取金額(億)、融券取張數，各附前日變化。"""
    r = sess.get(MI_MARGN_URL, params={"response": "json", "date": ymd, "selectType": "ALL"},
                 timeout=CONFIG["HTTP_TIMEOUT"])
    j = r.json()
    if j.get("stat") != "OK":
        print(f"  [MI_MARGN] stat={j.get('stat')}（{ymd}）"); return None
    tables = j.get("tables") or []
    summ = tables[0] if tables else None
    if not summ:
        return None
    fields = summ.get("fields", []); data = summ.get("data", [])
    print(f"  [MI_MARGN] fields={fields}")
    for row in data:
        print(f"  [MI_MARGN] row={row}")

    def col(sub):
        for i, f in enumerate(fields):
            if sub in str(f):
                return i
        return None
    ci_today, ci_prev = col("今日餘額"), col("前日餘額")
    if ci_today is None:
        return None

    def find(keys, exclude=()):
        for row in data:
            nm = str(row[0]).replace(" ", "")
            if all(k in nm for k in keys) and not any(x in nm for x in exclude):
                return row
        return None

    def bal_chg(row, scale=1.0):
        if not row:
            return None, None
        t = _num2(row[ci_today]); p = _num2(row[ci_prev]) if ci_prev is not None else None
        if t is None:
            return None, None
        return (round(t / scale, 1), round((t - p) / scale, 1) if p is not None else None)

    # 融資餘額：優先金額(仟元→億)，否則交易單位(張)
    fin_row = find(["融資", "仟元"]) or find(["融資", "金額"])
    fin_unit = "億"
    if fin_row:
        fin_bal, fin_chg = bal_chg(fin_row, 1e5)        # 仟元 → 億
    else:
        fin_row = find(["融資", "單位"]) or find(["融資"], exclude=["券", "仟元", "金額"])
        fin_unit = "張"; fin_bal, fin_chg = bal_chg(fin_row, 1.0)
    # 融券餘額：交易單位(張)
    sh_row = find(["融券", "單位"]) or find(["融券"], exclude=["資", "仟元", "金額"])
    short_bal, short_chg = bal_chg(sh_row, 1.0)
    out = {"date": ymd if "-" in ymd else _fmt_date(ymd),
           "fin_bal": fin_bal, "fin_chg": fin_chg, "fin_unit": fin_unit,
           "short_bal": short_bal, "short_chg": short_chg}
    print(f"  [MI_MARGN] parsed={out}")
    return out


def fetch_txf_foreign(sess):
    """外資台指期淨未平倉口數（TAIFEX OpenAPI；只回最新交易日）。負數=淨空。"""
    r = sess.get(TAIFEX_FUT_URL, headers=TAIFEX_HEADERS, timeout=CONFIG["HTTP_TIMEOUT"])
    data = r.json()
    if not isinstance(data, list) or not data:
        print("  [TAIFEX] 無資料或格式非預期"); return None
    date = data[0].get("Date", "")
    for it in data:
        cc = str(it.get("ContractCode", "")); item = str(it.get("Item", ""))
        if ("臺股期貨" in cc or "台股期貨" in cc) and "外資" in item:
            out = {"date": _fmt_date(date),
                   "net_oi": _num2(it.get("OpenInterest(Net)")),
                   "long_oi": _num2(it.get("OpenInterest(Long)")),
                   "short_oi": _num2(it.get("OpenInterest(Short)"))}
            print(f"  [TAIFEX] {out}")
            return out
    cset = sorted(set(str(x.get("ContractCode", "")) for x in data))[:8]
    print(f"  [TAIFEX] 找不到臺股期貨/外資。可見契約樣本={cset}")
    return None


def build_market_extras(sess, con):
    """彙整三大法人 / 融資融券 / 外資台指期，回傳 dict（各區塊失敗則為 None）。"""
    row = con.execute("SELECT MAX(date) FROM price").fetchone()
    latest = row[0] if row else None
    ymd = latest.replace("-", "") if latest else dt.date.today().strftime("%Y%m%d")
    out = {"date": latest or _fmt_date(ymd)}
    for key, fn in (("inst3", lambda: fetch_inst3(sess, ymd)),
                    ("margin", lambda: fetch_margin(sess, ymd)),
                    ("txf_foreign", lambda: fetch_txf_foreign(sess))):
        try:
            out[key] = fn()
        except Exception as e:
            print(f"  法人動向[{key}] 抓取/解析失敗：{e}")
            out[key] = None
    return out


def output_market_extras(extras):
    os.makedirs(CONFIG["OUTPUT_DIR"], exist_ok=True)
    d = (extras.get("date") or dt.date.today().isoformat()).replace("-", "")
    path = os.path.join(CONFIG["OUTPUT_DIR"], f"extras_{d}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(extras, f, ensure_ascii=False)
    print(f"已輸出法人動向：{path}")


# ============================================================
#  資金流向：大戶(三大法人合計) / 投信，當日 + 近5/20/60日 買賣超 TOP10（依金額億）
# ============================================================
FLOW_WINDOW = 60        # 近 N 個交易日累計（最長窗）
FLOW_TOPN = 10
# (網頁端後綴, 交易日數)；當日以 1 表示
FLOW_WINDOWS = (("d", 1), ("5", 5), ("20", 20), ("60", 60))


def build_flows(con):
    """以 inst 表(三大法人/投信張數) × 當日收盤估算買賣超金額(億)，
    產出 大戶/投信 各『當日 / 近5日 / 近20日 / 近60日累計』的買超/賣超 TOP10。"""
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM inst WHERE total_lots IS NOT NULL "
        "ORDER BY date DESC LIMIT ?", (FLOW_WINDOW,))]
    if not dates:
        print("資金流向：inst 尚無三大法人資料，略過（下次抓到後即產生）。")
        return None
    latest = dates[0]
    win_min = dates[-1]
    names = {r[0]: r[1] for r in con.execute("SELECT stock_id,name FROM stock")}

    # 最近一日漲跌%
    prev = con.execute("SELECT DISTINCT date FROM price WHERE date < ? ORDER BY date DESC LIMIT 1",
                       (latest,)).fetchone()
    chg = {}
    if prev:
        cur = dict(con.execute("SELECT stock_id,close FROM price WHERE date=?", (latest,)))
        prv = dict(con.execute("SELECT stock_id,close FROM price WHERE date=?", (prev[0],)))
        for sid, c in cur.items():
            p = prv.get(sid)
            if c is not None and p:
                chg[sid] = round((c - p) / p * 100, 2)

    def amounts(col, since):
        # 金額(億) = Σ 張 × 1000 × 收盤 / 1e8 = Σ 張 × 收盤 / 1e5
        rows = con.execute(
            f"SELECT i.stock_id, i.{col}, p.close FROM inst i "
            f"JOIN price p ON p.stock_id=i.stock_id AND p.date=i.date "
            f"WHERE i.date>=? AND i.{col} IS NOT NULL", (since,)).fetchall()
        out = {}
        for sid, lots, close in rows:
            if lots is None or close is None:
                continue
            out[sid] = out.get(sid, 0.0) + lots * close / 1e5
        return out

    def top(amts):
        items = list(amts.items())
        buy = sorted((x for x in items if x[1] > 0), key=lambda t: -t[1])[:FLOW_TOPN]
        sell = sorted((x for x in items if x[1] < 0), key=lambda t: t[1])[:FLOW_TOPN]
        mk = lambda lst: [{"sid": s, "name": names.get(s, ""), "amt": round(a, 1),
                           "chg": chg.get(s)} for s, a in lst]
        return {"buy": mk(buy), "sell": mk(sell)}

    def since_for(n):
        # 近 n 個交易日的起算日（dates 為由新到舊）；資料不足時取最舊一筆
        return dates[min(n, len(dates)) - 1]

    out = {"date": latest, "win_from": win_min, "win_days": len(dates)}
    for suf, n in FLOW_WINDOWS:
        since = latest if n == 1 else since_for(n)
        out[f"big_{suf}"] = top(amounts("total_lots", since))
        out[f"trust_{suf}"] = top(amounts("trust_lots", since))
    return out


def output_flows(flows):
    if not flows:
        return
    os.makedirs(CONFIG["OUTPUT_DIR"], exist_ok=True)
    d = (flows.get("date") or dt.date.today().isoformat()).replace("-", "")
    path = os.path.join(CONFIG["OUTPUT_DIR"], f"flows_{d}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(flows, f, ensure_ascii=False)
    print(f"已輸出資金流向 TOP10：{path}")


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

    # 1b) 個股日線『以 FinMind 為主』：逐交易日抓最近數日全市場日線覆蓋官方快照（較可靠）。
    #     成功則 FinMind 為準；失敗/無資料則沿用上面的官方 OpenAPI 快照。名稱仍由官方快照/DB 提供。
    if not args.skip_update and CONFIG["FINMIND_TOKEN"]:
        fm = fetch_finmind_prices_recent(CONFIG["FINMIND_TOKEN"])
        if fm is not None and not fm.empty:
            upsert_price(con, fm)
            snap_ids |= set(fm["stock_id"].astype(str))
            ndays = fm["date"].nunique()
            print(f"FinMind 全市場日線：更新/覆蓋 {len(fm)} 列、{ndays} 個交易日（最新日 {fm['date'].max()}）")
        else:
            print("FinMind 全市場日線：無資料，沿用官方 OpenAPI 快照。")

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

    # 發行張數（表頭發行/流通張數）：★放在深度回補『之前』，確保 FinMind 額度尚足（回補會大量用量、
    # 易把後面的請求擠到限流失敗，先前 deep_backfill 就因此讓表頭發行/流通張數空白）；失敗不影響主流程
    try:
        update_issued_shares(con, CONFIG["FINMIND_TOKEN"])
    except Exception as e:
        print(f"發行張數資料失敗（不影響主流程）：{e}")

    # 上櫃(OTC)主力『近端每日更新』：T86 只含上市，上櫃改用 FinMind 逐檔補近端，
    # 讓上櫃個股K線下圖『主力買賣超』每天跟上（放在深度回補之前；失敗不影響主流程）
    try:
        update_inst_otc(con, CONFIG["FINMIND_TOKEN"])
    except Exception as e:
        print(f"上櫃主力近端更新失敗（不影響主流程）：{e}")

    # 主力/外資/投信『深度歷史』回補到 HISTORY_START（FinMind，分批；失敗不影響主流程）
    try:
        backfill_inst_history(con, CONFIG["FINMIND_TOKEN"])
    except Exception as e:
        print(f"主力歷史回補失敗（不影響主流程）：{e}")

    # 集保 400 張大戶持股%（週更新；供個股K線副圖，失敗不影響主流程）
    try:
        update_shareholding(con, CONFIG["FINMIND_TOKEN"])
    except Exception as e:
        print(f"集保大戶資料失敗（不影響主流程）：{e}")

    # 資金流向 TOP10（大戶=三大法人合計 / 投信；當日 + 近5/20/60日）
    try:
        output_flows(build_flows(con))
    except Exception as e:
        print(f"資金流向產出失敗（不影響主流程）：{e}")

    # ⑦ 法人動向：三大法人 / 融資融券 / 外資台指期（全新資料源，失敗不影響主流程）
    try:
        extras = build_market_extras(sess, con)
        output_market_extras(extras)
    except Exception as e:
        print(f"法人動向資料失敗（不影響主流程）：{e}")

    # ⑧ 產業分類補底：抓 FinMind TaiwanStockInfo 大分類快取進 industry 表（每天若已有快取即略過）
    try:
        import tw_industry
        nind = tw_industry.fetch_finmind_industry(con, CONFIG["FINMIND_TOKEN"])
        if nind:
            print(f"FinMind TaiwanStockInfo：產業分類＋股名/市場 補齊 {nind} 檔。")
    except Exception as e:
        print(f"產業分類補底失敗（不影響主流程，curated 表仍可用）：{e}")

    con.close()


if __name__ == "__main__":
    main()
