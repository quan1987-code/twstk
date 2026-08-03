# -*- coding: utf-8 -*-
"""
台股「月均量爆量起漲」每日選股程式  v2（全免費官方資料版）
================================================================
本程式所有資料一律取自「免費、官方、免 token」的端點，不需要 FinMind（含免費版）。
替代來源集中在 `tw_free_sources.py`，本檔只負責組裝與寫入 twstock.db。

  ● 當日全市場價量 = 證交所 + 櫃買官方 OpenAPI（各 1 次請求）
        上市：https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
        上櫃：https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes
        （兩者都只提供「最新一天」，自帶民國日期，數字乾淨無逗號）
  ● 近端補洞／歷史回補 = 證交所 MI_INDEX、櫃買上櫃行情（全市場單日，可查任一交易日）
        新上市個股的深度回補另用 STOCK_DAY／櫃買個股月線（逐檔單月，一次一整個月）。
  ● 三大法人買賣超 = 證交所 T86（上市）＋ 櫃買三大法人日報（上櫃），皆為「全市場單日」，
        一天 2 個請求即涵蓋全市場，上市/上櫃都能每日更新，深度歷史也用同一路徑回補。
  ● 400 張大戶持股% / 發行股數 = 集保結算所 TDCC 開放資料（全市場、週更新）。
  ● 產業別／股名／市場別 = 證交所、櫃買公司基本資料 t187ap03（退回 ISIN 服務）。

選股邏輯（與 v1 相同，已驗證）：
  硬性條件：① 爆量(今日量≥N倍月均量20日) ② 站上月線 ③ 價漲量增(收紅)
            ④ 流動性(月均量、成交額、股價門檻) ⑤ 位階不過高(季線乖離上限)
  加分排序：爆量強度、量能持續、突破季高、月線翻揚、站上季線、季線翻揚、多頭排列、站上年線

使用方式：
  1) pip install requests pandas numpy openpyxl     （不需要 finmind 套件、不需要任何 token）
  2) 首次執行：自動抓當日 + 用官方端點逐檔回補歷史
        python tw_volume_breakout_screener_v2.py
     首次回補受證交所流量限制（約 3 次/5 秒），會分批進行；中斷後重跑會自動接續。
     想一次補多一點可調高環境變數 PRICE_BACKFILL_PER_RUN。
  3) 之後每天執行：官方 OpenAPI 抓當日 + 選股（數秒完成）。

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

import tw_free_sources as fs   # 免費官方來源（取代 FinMind 各資料表）

# 新版 Python(3.13+)的 OpenSSL 對憑證檢查很嚴格，部分政府網站(如櫃買 tpex.org.tw)
# 的憑證缺少 Subject Key Identifier 欄位而被拒。對「公開、唯讀」的政府開放資料端點
# 關閉憑證驗證是安全且常見的作法，這裡先關閉相關警告訊息。
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
#  CONFIG
# ============================================================
CONFIG = {
    "DB_PATH": "twstock.db",
    "OUTPUT_DIR": "output",
    "BACKFILL_DAYS": 400,        # （備用）回補日曆天數
    "BACKFILL_START": "2005-01-01",  # 個股歷史深度回補起始日（補到 2005 年初）
    "BACKFILL_MIN_ROWS": 60,     # 個股 DB 內少於此天數就觸發回補（普通是首次）
    "FRESH_DAYS": 7,             # 選股時：個股最新一筆超過幾天前就視為停牌/已下市，排除
    "HTTP_TIMEOUT": 30,
    # 逐檔歷史回補（官方端點，受證交所約 3 次/5 秒限流）：每次 run 的個股數與月數上限。
    # 平時排程用小值逐步補；想一次補完可用 daily.yml 的 deep_backfill 拉高。
    "PRICE_BACKFILL_PER_RUN": int(os.environ.get("PRICE_BACKFILL_PER_RUN", "8") or "8"),
    "PRICE_BACKFILL_MAX_MONTHS": int(os.environ.get("PRICE_BACKFILL_MAX_MONTHS", "36") or "36"),
    # 近端補洞：確認最近 N 個交易日的全市場價量都在 DB 內（官方 OpenAPI 只給最新一天）
    "RECENT_FILL_DAYS": int(os.environ.get("RECENT_FILL_DAYS", "5") or "5"),
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
    # 這些是公開唯讀資料，關閉憑證驗證以確保可連線。
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


def fill_recent_days(con, sess, want_days=None):
    """近端補洞：官方 OpenAPI 只提供「最新一天」，若程式某天沒跑（假日排程、Actions 失敗），
    中間的交易日就會缺。改用證交所 MI_INDEX／櫃買上櫃行情（可查任一交易日的全市場）把
    最近 want_days 個交易日補齊。原本這件事是靠 FinMind 逐日全市場查詢，免費版已不可行。
    回傳補進的資料列數。"""
    want_days = want_days or CONFIG["RECENT_FILL_DAYS"]
    have = set(r[0] for r in con.execute(
        "SELECT DISTINCT date FROM price ORDER BY date DESC LIMIT 40"))
    total = 0
    d = dt.date.today()
    checked = 0
    for _ in range(want_days * 3):               # 往回掃，跳過週末/休市
        if checked >= want_days:
            break
        if d.weekday() < 5:
            iso = d.isoformat()
            checked += 1
            if iso not in have:
                try:
                    rows = fs.fetch_market_day(sess, iso)
                except Exception as e:
                    print(f"  近端補洞 {iso} 失敗：{e}")
                    rows = []
                rows = [r for r in rows if is_common_stock(r["stock_id"])]
                if rows:
                    con.executemany(
                        "INSERT OR REPLACE INTO price VALUES (?,?,?,?,?,?,?,?)",
                        [(r["stock_id"], r["date"], r["open"], r["high"], r["low"],
                          r["close"], r["volume"], r["amount"]) for r in rows])
                    con.executemany(
                        "INSERT OR IGNORE INTO stock(stock_id,name,market) VALUES (?,?,?)",
                        [(r["stock_id"], r["name"], r["market"]) for r in rows if r["name"]])
                    con.commit()
                    total += len(rows)
                    print(f"  近端補洞 {iso}：{len(rows)} 檔")
        d -= dt.timedelta(days=1)
    if total:
        print(f"近端補洞完成：補進 {total} 列（官方全市場單日行情）。")
    else:
        print("近端補洞：最近交易日皆已在 DB，無需補抓。")
    return total


# ============================================================
#  歷史回補：官方逐檔單月（證交所 STOCK_DAY / 櫃買個股月線）
# ============================================================
def backfill_one(sess, stock_id, market, start, end, max_months=0, stats=None):
    """用官方端點抓單檔歷史，回傳 normalized rows（list of tuples）。"""
    rows = fs.fetch_stock_history(sess, stock_id, market, start, end,
                                  max_months=max_months, stats=stats)
    return [(r["stock_id"], r["date"], r["open"], r["high"], r["low"],
             r["close"], r["volume"], r["amount"]) for r in rows]


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


def run_backfill(con, sess, universe, args):
    """對尚未『深度回補到起始日』的個股做一次性回補（補到 BACKFILL_START）。
    來源＝證交所 STOCK_DAY／櫃買個股月線（免費官方，一次一整個月），取代原本的 FinMind 逐檔。
    官方端點有流量限制（約 3 次/5 秒），故每次 run 只補 PRICE_BACKFILL_PER_RUN 檔、
    每檔最多 PRICE_BACKFILL_MAX_MONTHS 個月；deep_done 記錄已補到哪個月，跨次接續。
    絕大多數個股在 DB 快取中早已補齊，平時只有『新上市股』會走到這裡。"""
    con.execute("CREATE TABLE IF NOT EXISTS deep_done(stock_id TEXT PRIMARY KEY)")
    con.execute("CREATE TABLE IF NOT EXISTS deep_progress(stock_id TEXT PRIMARY KEY, earliest TEXT)")
    con.commit()
    end = dt.date.today().isoformat()
    start = CONFIG.get("BACKFILL_START") or (dt.date.today() - dt.timedelta(days=CONFIG["BACKFILL_DAYS"])).isoformat()
    done = set(r[0] for r in con.execute("SELECT stock_id FROM deep_done"))
    todo = sorted(s for s in universe if s not in done and is_common_stock(s))
    if not todo:
        print(f"個股歷史已深度回補（至 {start}），略過回補。"); return

    mkt = {r[0]: (r[1] or "") for r in con.execute("SELECT stock_id, market FROM stock")}
    prog = {r[0]: r[1] for r in con.execute("SELECT stock_id, earliest FROM deep_progress")}
    cap = args.max_backfill or CONFIG["PRICE_BACKFILL_PER_RUN"]
    todo_total = len(todo)
    todo = todo[:cap]
    months_cap = CONFIG["PRICE_BACKFILL_MAX_MONTHS"]
    print(f"個股歷史深度回補（官方端點）：待補 {todo_total} 檔，本次 {len(todo)} 檔"
          f"（目標 {start}，每檔每次最多 {months_cap} 個月；其餘後續 run 逐步補齊）…")
    for i, sid in enumerate(todo, 1):
        # 已補到的最舊月份 → 這次從它的前一個月往回接續（首次從今天往回）
        cur_end = prog.get(sid) or end
        if cur_end <= start:
            con.execute("INSERT OR IGNORE INTO deep_done VALUES (?)", (sid,)); con.commit(); continue
        stats = {}
        try:
            rows = backfill_one(sess, sid, mkt.get(sid, ""), start, cur_end,
                                max_months=months_cap, stats=stats)
        except Exception as e:
            print(f"  [{i}/{len(todo)}] {sid} 失敗：{e}（稍後重跑接續）")
            continue
        if rows:
            con.executemany("INSERT OR REPLACE INTO price VALUES (?,?,?,?,?,?,?,?)", rows)
        got_earliest = min((r[1] for r in rows), default=None)
        # 這一輪往回掃了 months_cap 個月：把游標推到那個區間的起點，下一輪從那裡再往回
        cur_dt = dt.date.fromisoformat(cur_end[:10])
        step_y, step_m = cur_dt.year, cur_dt.month
        for _ in range(months_cap):
            step_y, step_m = (step_y - 1, 12) if step_m == 1 else (step_y, step_m - 1)
        new_cursor = max(start, f"{step_y:04d}-{step_m:02d}-28")
        if got_earliest:
            new_cursor = min(new_cursor, got_earliest)
        con.execute("INSERT OR REPLACE INTO deep_progress VALUES (?,?)", (sid, new_cursor))
        # 完成條件：補到目標起始日／這一輪整段無資料／已掃到連續數月無資料（＝上市前）
        finished = (new_cursor <= start) or (not rows) or stats.get("early_stop")
        if finished:
            con.execute("INSERT OR IGNORE INTO deep_done VALUES (?)", (sid,))
        con.commit()
        print(f"  回補進度 [{i}/{len(todo)}]  {sid}({mkt.get(sid,'?')}) 寫入 {len(rows)} 筆，"
              + ("已補齊（掃到上市前）" if finished else f"游標 → {new_cursor}"))
    left = todo_total - len(set(r[0] for r in con.execute("SELECT stock_id FROM deep_done")) & set(todo))
    print(f"本輪回補結束。尚餘約 {max(left, 0)} 檔待回補（下次執行續抓）。"
          if left > 0 else "本輪回補結束。全部個股已深度回補完成。")


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
#  三大法人買賣超（籌碼）：上市 = 證交所 T86、上櫃 = 櫃買三大法人日報
#  兩者都是「全市場單日」端點，一天 2 個請求涵蓋全市場，近端更新與深度回補共用同一路徑。
# ============================================================
# ⑦ 法人動向：三大法人金額(BFI82U) / 融資融券(MI_MARGN) / 外資台指期(TAIFEX OpenAPI)
BFI82U_URL = "https://www.twse.com.tw/fund/BFI82U"
MI_MARGN_URL = "https://www.twse.com.tw/exchangeReport/MI_MARGN"
TAIFEX_FUT_URL = ("https://openapi.taifex.com.tw/v1/"
                  "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate")
TAIFEX_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
TRUST_LOOKBACK = 30      # 連買候選觀察窗（最近約一個月）
INST_LOOKBACK = 250      # 每日前向更新的交易日數（近端；更深的歷史另由 backfill_inst_history 補）
TRUST_BASE_THR = 50      # 候選基準門檻(張)，網頁端可往上切換到 100/200/500/1000
TRUST_MIN_STREAK = 3     # 連續買超天數門檻

# 個股K線副圖「深度歷史」目標起始日。以「快取 DB＋每次 run 上限」分批補齊，不影響每日主流程；
# 上限可用環境變數調高做一次性長跑（見 daily.yml 的 deep_backfill）。
# 主力(三大法人)＝T86(上市)＋櫃買日報(上櫃)『全市場單日』逐日補；400大戶＝TDCC 全市場週抓。
HISTORY_START = os.environ.get("HISTORY_START", "2019-01-01") or "2019-01-01"           # 400張大戶回補起點
INST_HISTORY_START = os.environ.get("INST_HISTORY_START", "2020-01-01") or "2020-01-01"  # 主力(三大法人)回補起點
# 每次 run 逐日回補的『交易日數』上限（1 日 = 上市+上櫃各 1 個請求，涵蓋全市場所有個股）
INST_BACKFILL_PER_RUN = int(os.environ.get("INST_BACKFILL_PER_RUN", "150") or "150")
SHAREHOLD_MAX_PER_RUN = int(os.environ.get("SHAREHOLD_MAX_PER_RUN", "12") or "12")     # 每次 run 回補週數上限


def _ensure_inst_tables(con):
    """inst（三大法人買賣超）與 inst_day（該交易日該市場是否已抓過）。"""
    con.execute("CREATE TABLE IF NOT EXISTS inst("
                "stock_id TEXT, date TEXT, trust_lots REAL, PRIMARY KEY(stock_id,date))")
    for col in ("foreign_lots", "dealer_lots", "total_lots"):
        try:
            con.execute(f"ALTER TABLE inst ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass   # 欄位已存在
    con.execute("CREATE TABLE IF NOT EXISTS inst_day("
                "date TEXT, market TEXT, PRIMARY KEY(date, market))")
    con.execute("CREATE TABLE IF NOT EXISTS inst_day_miss("
                "date TEXT, market TEXT, tries INTEGER, PRIMARY KEY(date, market))")
    con.commit()
    _seed_inst_day(con)


def _seed_inst_day(con):
    """一次性：用 DB 內既有的 inst 資料回填 inst_day 完成標記。
    舊版（FinMind 逐檔）沒有這張表，若不回填，改版後第一次跑會把 2020 年以來每個交易日
    全部重抓一次。判定方式：該(日,市場)已有 inst 資料的個股數 ≥ 當日該市場有股價的個股數一半，
    才視為已抓齊（上櫃過去覆蓋率參差，未達標者仍會被重抓補滿）。"""
    if con.execute("SELECT 1 FROM inst_day WHERE date='__seeded__'").fetchone():
        return
    try:
        rows = con.execute("""
            SELECT p.date, s.market,
                   COUNT(DISTINCT i.stock_id) AS have,
                   COUNT(DISTINCT p.stock_id) AS tot
              FROM price p
              JOIN stock s ON s.stock_id = p.stock_id
              LEFT JOIN inst i ON i.stock_id = p.stock_id AND i.date = p.date
                              AND i.total_lots IS NOT NULL
             WHERE s.market IN ('上市','上櫃')
             GROUP BY p.date, s.market""").fetchall()
    except sqlite3.Error:
        rows = []
    seeded = [(d, m) for d, m, have, tot in rows if tot and have >= tot * 0.5]
    if seeded:
        con.executemany("INSERT OR IGNORE INTO inst_day VALUES (?,?)", seeded)
    con.execute("INSERT OR IGNORE INTO inst_day VALUES ('__seeded__','-')")
    con.commit()
    if seeded:
        print(f"三大法人：以既有 DB 資料回填完成標記 {len(seeded)} 筆(日×市場)，避免重抓。")


INST_MISS_GIVEUP = 3        # 舊日期連抓幾次都無資料就放棄（避免每天重試永遠拿不到的日子）
INST_MISS_FRESH_DAYS = 30   # 近 N 天的日期不套用放棄規則（交易所可能只是還沒公布）


def _fetch_inst_into_db(con, sess, dates, label):
    """把指定交易日的三大法人買賣超（上市 T86 + 上櫃櫃買日報）寫進 inst 表。
    以 inst_day 記錄「該日該市場已抓過」，跨次 run 不重抓；回傳實際寫入的交易日數。
    抓不到的舊日期（例如交易所該市場當年尚無此報表）累計 INST_MISS_GIVEUP 次後放棄，
    但近 INST_MISS_FRESH_DAYS 天內的日期永遠會重試（可能只是還沒公布）。"""
    done = set(con.execute("SELECT date, market FROM inst_day"))
    miss = {(d, m): t for d, m, t in con.execute("SELECT date, market, tries FROM inst_day_miss")}
    fresh_after = (dt.date.today() - dt.timedelta(days=INST_MISS_FRESH_DAYS)).isoformat()
    n = 0
    for d in dates:
        need = [m for m in ("上市", "上櫃")
                if (d, m) not in done
                and (d >= fresh_after or miss.get((d, m), 0) < INST_MISS_GIVEUP)]
        if not need:
            continue
        rows, ok = [], set()
        try:
            if "上市" in need:
                r1 = fs.fetch_twse_inst_day(sess, d)
                if r1:
                    rows += r1
                    ok.add("上市")
                time.sleep(fs.TWSE_SLEEP)
            if "上櫃" in need:
                r2 = fs.fetch_tpex_inst_day(sess, d)
                if r2:
                    rows += r2
                    ok.add("上櫃")
                time.sleep(fs.TPEX_SLEEP)
        except Exception as e:
            print(f"  {label} {d} 失敗：{e}")
            continue
        rows = [r for r in rows if is_common_stock(r[0])]
        if rows:
            con.executemany(
                "INSERT OR REPLACE INTO inst"
                "(stock_id,date,foreign_lots,trust_lots,dealer_lots,total_lots)"
                " VALUES (?,?,?,?,?,?)", rows)
            n += 1
        # 只把「確實成功取得」的市場標記完成；沒抓到的累計失敗次數，達上限就不再重試
        if ok:
            con.executemany("INSERT OR IGNORE INTO inst_day VALUES (?,?)", [(d, m) for m in ok])
        for m in need:
            if m not in ok:
                con.execute("INSERT INTO inst_day_miss(date,market,tries) VALUES (?,?,1) "
                            "ON CONFLICT(date,market) DO UPDATE SET tries=tries+1", (d, m))
        con.commit()
    return n


def _inst_pending_dates(con, dates):
    """挑出還需要抓三大法人的交易日（任一市場未完成、且未被判定為永久無資料）。"""
    done = set(con.execute("SELECT date, market FROM inst_day"))
    miss = {(d, m): t for d, m, t in con.execute("SELECT date, market, tries FROM inst_day_miss")}
    fresh_after = (dt.date.today() - dt.timedelta(days=INST_MISS_FRESH_DAYS)).isoformat()
    out = []
    for d in dates:
        for m in ("上市", "上櫃"):
            if (d, m) in done:
                continue
            if d < fresh_after and miss.get((d, m), 0) >= INST_MISS_GIVEUP:
                continue
            out.append(d)
            break
    return out


def update_inst(con, sess):
    """把最近 INST_LOOKBACK 個交易日的『三大法人(外資/投信/自營/合計)買賣超』補進 inst 表。
    上市＝證交所 T86、上櫃＝櫃買三大法人日報，兩者都是全市場單日端點，
    所以上市/上櫃個股的主力副圖都能每日更新（原本上櫃要靠 FinMind 逐檔，免費版已不可行）。"""
    _ensure_inst_tables(con)
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM price ORDER BY date DESC LIMIT ?", (INST_LOOKBACK,))]
    todo = sorted(_inst_pending_dates(con, dates))
    if not todo:
        print("三大法人買賣超：已是最新。")
        return
    print(f"更新三大法人買賣超：需抓 {len(todo)} 個交易日（上市 T86 ＋ 上櫃櫃買日報）…")
    n = _fetch_inst_into_db(con, sess, todo, "三大法人")
    print(f"三大法人買賣超更新完成：新增/補齊 {n} 個交易日。")


def backfill_inst_history(con, sess):
    """把『三大法人買賣超』回補到 INST_HISTORY_START，涵蓋上市＋上櫃。
    改為『逐交易日、全市場』抓取（上市 T86 + 上櫃櫃買日報）：一天 2 個請求就涵蓋所有個股，
    比原本 FinMind 逐檔（1 檔 1 請求、約 1700 檔）快兩個數量級，也完全不需要付費方案。
    以 inst_day 記錄已完成的(日期,市場)，每次 run 最多補 INST_BACKFILL_PER_RUN 個交易日。"""
    _ensure_inst_tables(con)
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM price WHERE date >= ? ORDER BY date DESC",
        (INST_HISTORY_START,))]
    todo = _inst_pending_dates(con, dates)
    if not todo:
        print(f"主力歷史回補：已補齊至 {INST_HISTORY_START}。")
        return
    todo_total = len(todo)
    todo = sorted(todo[:INST_BACKFILL_PER_RUN])   # 由新往舊挑，再依日期順序抓
    print(f"主力歷史逐日回補(上市+上櫃全市場)：待補 {todo_total} 個交易日，本次 {len(todo)} 日"
          f"（目標 {INST_HISTORY_START}；其餘後續 run 逐步補齊）…")
    n = _fetch_inst_into_db(con, sess, todo, "主力歷史")
    print(f"主力歷史逐日回補完成：本次 {n} 個交易日（尚餘約 {max(todo_total - len(todo), 0)} 日）。")


def update_shareholding_and_issued(con, sess):
    """集保股權分散 → ①『400張以上大戶持股比率(%)』週資料（個股K線副圖）
                      ②『發行張數』（表頭發行/流通張數）。

    來源改為集保結算所 TDCC 開放資料（https://opendata.tdcc.com.tw/getOD.aspx?id=1-5）：
    免費、免 token、一個請求就拿到全市場最新一週的完整級距表，取代原本 Sponsor 才有的
    FinMind `TaiwanStockHoldingSharesPer`。
      ● 400張大戶% = 持股分級 12~15（400,001 股以上）的「佔集保庫存比例%」加總。
      ● 發行張數  = 合計級距的股數（集保總股數），與 400張大戶% 同源同分母，
                    流通張數 = 發行 ×(1−400張大戶%) 於網頁端計算最一致。
    TDCC 只提供最新一週，歷史週次無免費全市場端點；DB 既有的歷史仍保留，
    之後每週自動累積一筆，時間拉長即恢復完整曲線。
    失敗不影響主流程（呼叫端包 try/except）。"""
    con.execute("CREATE TABLE IF NOT EXISTS shareholding("
                "stock_id TEXT, date TEXT, big400_pct REAL, PRIMARY KEY(stock_id,date))")
    con.execute("CREATE TABLE IF NOT EXISTS stockmeta("
                "stock_id TEXT PRIMARY KEY, issued_lots REAL, updated TEXT)")
    con.commit()

    snap = fs.fetch_tdcc_shareholding(sess)
    if not snap:
        print("集保大戶／發行張數：TDCC 無資料或抓取失敗，沿用 DB 既有資料。")
        return
    have = set(r[0] for r in con.execute("SELECT DISTINCT date FROM shareholding"))
    weeks = sorted(snap)
    new_weeks = [d for d in weeks if d not in have][-SHAREHOLD_MAX_PER_RUN:]
    n_big = 0
    for d in new_weeks:
        rows = [(sid, d, rec["big400"]) for sid, rec in snap[d].items()
                if is_common_stock(sid) and rec.get("big400")]
        if rows:
            con.executemany(
                "INSERT OR REPLACE INTO shareholding(stock_id,date,big400_pct) VALUES (?,?,?)", rows)
            con.commit()
            n_big += 1
            print(f"  集保 {d}：{len(rows)} 檔 400 張大戶%")
    con.execute("DELETE FROM shareholding WHERE date < ?", (HISTORY_START,))
    con.commit()
    if not new_weeks:
        print(f"集保大戶：已是最新（最新週 {weeks[-1]}）。")
    else:
        print(f"集保大戶更新完成：新增 {n_big} 週（最新週 {weeks[-1]}）。")

    # 發行張數：取最新一週的集保合計股數；缺漏者退回公司基本資料的已發行股數
    latest = weeks[-1]
    rows = [(sid, round(rec["total_shares"] / 1000.0), latest)
            for sid, rec in snap[latest].items()
            if len(sid) == 4 and sid.isdigit() and rec.get("total_shares")]
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO stockmeta(stock_id,issued_lots,updated) VALUES (?,?,?)", rows)
        con.commit()
    print(f"發行張數更新完成：{len(rows)} 檔（TDCC 集保總股數，週 {latest}）。")
    _fill_issued_from_company_meta(con, sess, latest)


def _fill_issued_from_company_meta(con, sess, tag):
    """TDCC 沒涵蓋到的個股（如剛上市），用證交所／櫃買公司基本資料 t187ap03 的
    已發行股數（或實收資本額÷面額）補上，仍是免費官方來源。"""
    missing = [r[0] for r in con.execute(
        "SELECT DISTINCT p.stock_id FROM price p "
        "LEFT JOIN stockmeta m ON m.stock_id = p.stock_id "
        "WHERE m.issued_lots IS NULL")]
    missing = [s for s in missing if is_common_stock(s)]
    if not missing:
        return
    try:
        meta = fs.fetch_company_meta(sess)
    except Exception as e:
        print(f"  發行張數補漏：公司基本資料抓取失敗（{e}）。")
        return
    rows = [(sid, round(meta[sid]["issued_shares"] / 1000.0), f"{tag}(t187ap03)")
            for sid in missing
            if sid in meta and meta[sid].get("issued_shares")]
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO stockmeta(stock_id,issued_lots,updated) VALUES (?,?,?)", rows)
        con.commit()
    print(f"  發行張數補漏：{len(rows)}/{len(missing)} 檔改由公司基本資料補上。")


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
                    help="本次最多回補幾檔(0=用 PRICE_BACKFILL_PER_RUN 預設值)")
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

    # 1b) 近端補洞：官方 OpenAPI 只給「最新一天」，若某天沒跑就會缺洞。
    #     用證交所 MI_INDEX／櫃買上櫃行情（可查任一交易日的全市場）把最近數個交易日補齊。
    if not args.skip_update:
        try:
            fill_recent_days(con, sess)
        except Exception as e:
            print(f"近端補洞失敗（不影響主流程）：{e}")

    # 標的清單：資料庫 ∪ 當日快照（首次執行資料庫為空，靠快照）
    info_all = pd.read_sql("SELECT * FROM stock", con)
    universe = set(info_all["stock_id"]) | snap_ids
    if not universe:
        print("無可用標的（首次執行卻抓不到當日資料）。請稍後重新執行。")
        write_empty_csv(); con.close(); return
    info_map = {r.stock_id: (r.name, r.market) for r in info_all.itertuples()}

    # 2) 歷史回補（官方逐檔單月；分批接續）
    if not args.skip_update:
        try:
            run_backfill(con, sess, universe, args)
        except Exception as e:
            print(f"個股歷史回補失敗（不影響主流程）：{e}")

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

    # 集保 400 張大戶持股% ＋ 發行張數（TDCC 開放資料，週更新；失敗不影響主流程）
    try:
        update_shareholding_and_issued(con, sess)
    except Exception as e:
        print(f"集保大戶／發行張數資料失敗（不影響主流程）：{e}")

    # 主力/外資/投信『深度歷史』回補到 INST_HISTORY_START（逐交易日全市場，分批；失敗不影響主流程）
    try:
        backfill_inst_history(con, sess)
    except Exception as e:
        print(f"主力歷史回補失敗（不影響主流程）：{e}")

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

    # ⑧ 產業分類補底：抓證交所／櫃買公司基本資料，大分類快取進 industry 表（有快取即略過）
    try:
        import tw_industry
        nind = tw_industry.fetch_official_industry(con, sess)
        if nind:
            print(f"公司基本資料(t187ap03)：產業分類＋股名/市場 補齊 {nind} 檔。")
    except Exception as e:
        print(f"產業分類補底失敗（不影響主流程，curated 表仍可用）：{e}")

    con.close()


if __name__ == "__main__":
    main()
