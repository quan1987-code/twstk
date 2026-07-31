# -*- coding: utf-8 -*-
r"""
tw_free_sources.py — 免費官方資料來源集中模組
================================================================
FinMind 由 Sponsor 降為「免費版」後，本站原本仰賴 Sponsor 才拿得到的資料改由
「免費、官方、免 token」的端點取得。所有替代來源集中在本模組，其餘程式只呼叫這裡。

| 原 FinMind 資料表 | 內容 | 免費版可用？ | 改用的來源 |
|---|---|---|---|
| `TaiwanStockPrice`(全市場單日) | 個股日線 | ✗ 全市場查詢受限 | TWSE `MI_INDEX`、TPEx 上櫃行情（全市場單日，含歷史） |
| `TaiwanStockPrice`(逐檔) | 個股日線歷史 | △ 逐檔可用但限流 | TWSE `STOCK_DAY`、TPEx 個股月線（逐檔單月） |
| `TaiwanStockInstitutionalInvestorsBuySell` | 三大法人買賣超 | △ 逐檔可用但限流 | TWSE `T86`(上市) + TPEx 三大法人日報(上櫃)，皆「全市場單日」 |
| `TaiwanStockHoldingSharesPer` | 集保股權分散(400張大戶%) | ✗ | 集保結算所 TDCC OpenData `1-5` |
| `TaiwanStockShareholding` | 發行股數 | ✗ | TDCC 合計股數；退回 TWSE/TPEx 公司基本資料(t187ap03) |
| `TaiwanStockInfo` | 產業別／股名／市場別 | △ | TWSE/TPEx `t187ap03`；退回 TWSE ISIN 服務 |
| `TaiwanVariousIndicators5Seconds` | 加權指數 TAIEX | ✗ | TWSE 發行量加權股價指數歷史 `MI_5MINS_HIST`（日 OHLC，比 5 秒快照更準） |
| `TaiwanStockDispositionSecuritiesPeriod` | 處置股起迄 | ✗ | TWSE `announcement/punish` + TPEx `tpex_disposal_information`（本地 DB 累積歷史） |
| `TaiwanStockTradingDailyReport` | 券商分點 | ✗ | 無免費官方 API（證交所 BSR 需驗證碼）→ 改以三大法人合計為主力／集中度基準 |

設計原則
  ● 政府端點改版頻繁：每個資料項都準備「新站 / 舊站」多組候選 URL，依序嘗試。
  ● 回傳格式歷代不同（`tables` / `aaData` / `data1..9`）：一律用 `_iter_tables()` 攤平，
    有欄名就依欄名對應、沒欄名才退回各端點的固定欄位順序。
  ● 政府開放資料端點憑證在新版 OpenSSL 下常驗證失敗（缺 Subject Key Identifier），
    這些是公開唯讀資料，統一以 `verify=False` 連線（與本專案其他檔案一致）。
"""

import csv
import io
import re
import time
import datetime as dt

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:                                    # pragma: no cover
    requests = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HTTP_TIMEOUT = 30

# 證交所對同一 IP 有「約 3 次 / 5 秒」的流量限制，逐檔/逐日抓取時務必間隔
TWSE_SLEEP = 2.0
TPEX_SLEEP = 1.0


# ============================================================
#  共用小工具
# ============================================================
def make_session():
    """建立共用 Session（政府開放資料端點：公開唯讀，關閉憑證驗證確保可連線）。"""
    if requests is None:
        raise RuntimeError("requests 未安裝")
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*"})
    s.verify = False
    return s


def num(x):
    """含逗號/空白/'--'/HTML 標籤的字串 → float；無法轉換回 None。"""
    if x is None:
        return None
    s = re.sub(r"<[^>]*>", "", str(x)).replace(",", "").replace("　", "").strip()
    if s in ("", "--", "---", "-", "N/A", "NA", "x", "X", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_to_iso(s):
    """民國/西元各種寫法 → ISO 'YYYY-MM-DD'；抓不到或月日不合理回 ''。
    支援 '1150623'、'115/06/23'、'115.06.23'、'115年06月23日'、'2026-06-23'、'20260623'。"""
    if s is None:
        return ""
    t = re.sub(r"<[^>]*>", "", str(s)).strip()
    if not t:
        return ""
    m = re.search(r"(19|20)(\d{2})[/.\-](\d{1,2})[/.\-](\d{1,2})", t)            # 西元帶分隔
    if m:
        return _iso(int(m.group(1) + m.group(2)), int(m.group(3)), int(m.group(4)))
    m = re.search(r"(\d{2,3})[年/.\-](\d{1,2})[月/.\-](\d{1,2})", t)              # 民國帶分隔
    if m:
        return _iso(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))
    d = re.sub(r"\D", "", t)
    if len(d) == 8:                                                              # 西元 20260623
        return _iso(int(d[:4]), int(d[4:6]), int(d[6:8]))
    if len(d) == 7:                                                              # 民國 1150623
        return _iso(int(d[:3]) + 1911, int(d[3:5]), int(d[5:7]))
    return ""


def _iso(y, mo, d):
    if 1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return ""


def iso_to_roc_slash(iso):
    """'2026-06-23' → '115/06/23'（TPEx 舊端點的日期格式）。"""
    y, m, d = int(iso[:4]), int(iso[5:7]), int(iso[8:10])
    return f"{y - 1911:03d}/{m:02d}/{d:02d}"


def iso_to_ymd(iso):
    """'2026-06-23' → '20260623'（TWSE 端點的日期格式）。"""
    return iso.replace("-", "")


def get_json(sess, url, params=None, timeout=HTTP_TIMEOUT, retries=2, backoff=3.0):
    """GET 回 JSON；失敗（連線/非 200/非 JSON）回 None，由呼叫端換下一組候選 URL。"""
    for k in range(retries):
        try:
            r = sess.get(url, params=params, timeout=timeout)
        except Exception:
            time.sleep(backoff * (k + 1))
            continue
        if r.status_code == 429:                     # 被證交所限流：多等一下再試
            time.sleep(backoff * 3 * (k + 1))
            continue
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except Exception:
            return None
    return None


def get_text(sess, url, params=None, timeout=HTTP_TIMEOUT, retries=2, encoding=None):
    """GET 回文字（CSV / HTML 用）；失敗回 None。"""
    for _ in range(retries):
        try:
            r = sess.get(url, params=params, timeout=timeout)
        except Exception:
            time.sleep(3)
            continue
        if r.status_code != 200:
            return None
        if encoding:
            r.encoding = encoding
        return r.text
    return None


def _iter_tables(j):
    """把 TWSE/TPEx 各世代回傳格式攤平成 [(fields|None, rows), ...]。
    涵蓋：{'tables':[{'fields','data'}]}、{'fields','data'}、{'aaData':[...]}、
    {'data1'..'data9','fields1'..'fields9'}。"""
    out = []
    if not isinstance(j, dict):
        return [(None, j)] if isinstance(j, list) else out
    if isinstance(j.get("tables"), list):
        for t in j["tables"]:
            if isinstance(t, dict) and isinstance(t.get("data"), list):
                out.append((t.get("fields") or t.get("field"), t["data"]))
    for key in ("data", "aaData"):
        if isinstance(j.get(key), list) and j[key]:
            out.append((j.get("fields") or j.get("field"), j[key]))
    for i in range(1, 10):                            # 舊 MI_INDEX：data1..data9 / fields1..fields9
        rows = j.get(f"data{i}")
        if isinstance(rows, list) and rows:
            out.append((j.get(f"fields{i}"), rows))
    return out


def _col(fields, *aliases):
    """依欄名找索引：先精準比對、再子字串比對。找不到回 None。"""
    if not fields:
        return None
    fs = [re.sub(r"\s|<[^>]*>", "", str(f)) for f in fields]
    for a in aliases:
        a2 = re.sub(r"\s", "", a)
        if a2 in fs:
            return fs.index(a2)
    for a in aliases:
        a2 = re.sub(r"\s", "", a)
        for i, f in enumerate(fs):
            if a2 and a2 in f:
                return i
    return None


def _cell(row, i):
    return row[i] if (i is not None and i < len(row)) else None


def _pick_table(tables, *must_have):
    """從多個表中挑出欄名同時含有 must_have 的那一張（找不到回 None）。"""
    for fields, rows in tables:
        if fields and all(_col(fields, m) is not None for m in must_have):
            return fields, rows
    return None


_CODE_RE = re.compile(r"^\d{4,6}$")


def _clean_code(x):
    s = re.sub(r"<[^>]*>|\s|　", "", str(x or ""))
    return s if _CODE_RE.fullmatch(s) else ""


# ============================================================
#  ① 全市場單日行情（取代 FinMind TaiwanStockPrice 的「全市場單日」查詢）
#     — 可查歷史任一交易日，是回補價量最有效率的來源（每日 2 個請求涵蓋全市場）
# ============================================================
TWSE_MI_INDEX_URLS = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
    "https://www.twse.com.tw/exchangeReport/MI_INDEX",
)
TPEX_QUOTES_URLS = (
    "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc",
    "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php",
)


def fetch_twse_market_day(sess, date_iso):
    """上市『每日收盤行情(全部)』單日全市場。
    回傳 [{stock_id,date,name,market,open,high,low,close,volume,amount}]；休市/失敗回 []。"""
    ymd = iso_to_ymd(date_iso)
    for url in TWSE_MI_INDEX_URLS:
        j = get_json(sess, url, {"date": ymd, "type": "ALLBUT0999", "response": "json"})
        if not j:
            continue
        if str(j.get("stat", "OK")).upper().startswith("很抱歉"):    # 該日無交易
            return []
        picked = _pick_table(_iter_tables(j), "證券代號", "收盤價")
        if not picked:
            continue
        fields, rows = picked
        ic = _col(fields, "證券代號")
        i_name = _col(fields, "證券名稱")
        i_o, i_h, i_l, i_c = (_col(fields, "開盤價"), _col(fields, "最高價"),
                              _col(fields, "最低價"), _col(fields, "收盤價"))
        i_v, i_a = _col(fields, "成交股數"), _col(fields, "成交金額")
        out = []
        for r in rows:
            sid = _clean_code(_cell(r, ic))
            if not sid:
                continue
            close = num(_cell(r, i_c))
            if close is None or close <= 0:
                continue
            out.append({"stock_id": sid, "date": date_iso, "market": "上市",
                        "name": re.sub(r"\s|　", "", str(_cell(r, i_name) or "")),
                        "open": num(_cell(r, i_o)), "high": num(_cell(r, i_h)),
                        "low": num(_cell(r, i_l)), "close": close,
                        "volume": num(_cell(r, i_v)), "amount": num(_cell(r, i_a))})
        if out:
            return out
    return []


# 舊版 TPEx『上櫃股票每日收盤行情』aaData 欄位順序（無欄名時的退路）
_TPEX_QUOTE_POS = {"code": 0, "name": 1, "close": 2, "chg": 3, "open": 4,
                   "high": 5, "low": 6, "volume": 7, "amount": 8}


def fetch_tpex_market_day(sess, date_iso):
    """上櫃全市場單日行情。回傳格式同 fetch_twse_market_day；休市/失敗回 []。"""
    for url in TPEX_QUOTES_URLS:
        if "stk_wn1430" in url:
            params = {"l": "zh-tw", "d": iso_to_roc_slash(date_iso), "se": "EW", "o": "json"}
        else:
            params = {"date": date_iso.replace("-", "/"), "type": "EW",
                      "id": "", "response": "json"}
        j = get_json(sess, url, params)
        if not j:
            continue
        out = []
        for fields, rows in _iter_tables(j):
            if not rows:
                continue
            if fields and _col(fields, "收盤") is not None:
                idx = {"code": _col(fields, "代號", "證券代號", "股票代號"),
                       "name": _col(fields, "名稱", "證券名稱"),
                       "open": _col(fields, "開盤"), "high": _col(fields, "最高"),
                       "low": _col(fields, "最低"), "close": _col(fields, "收盤"),
                       "volume": _col(fields, "成交股數", "成交仟股", "成交量"),
                       "amount": _col(fields, "成交金額", "成交仟元", "成交值")}
                vol_k = "仟" in str(fields[idx["volume"]]) if idx["volume"] is not None else False
                amt_k = "仟" in str(fields[idx["amount"]]) if idx["amount"] is not None else False
            else:
                idx, vol_k, amt_k = _TPEX_QUOTE_POS, True, True   # 舊端點單位為仟股/仟元
            for r in rows:
                sid = _clean_code(_cell(r, idx.get("code")))
                if not sid:
                    continue
                close = num(_cell(r, idx.get("close")))
                if close is None or close <= 0:
                    continue
                vol = num(_cell(r, idx.get("volume")))
                amt = num(_cell(r, idx.get("amount")))
                out.append({"stock_id": sid, "date": date_iso, "market": "上櫃",
                            "name": re.sub(r"\s|　", "", str(_cell(r, idx.get("name")) or "")),
                            "open": num(_cell(r, idx.get("open"))),
                            "high": num(_cell(r, idx.get("high"))),
                            "low": num(_cell(r, idx.get("low"))), "close": close,
                            "volume": (vol * 1000 if (vol is not None and vol_k) else vol),
                            "amount": (amt * 1000 if (amt is not None and amt_k) else amt)})
            if out:
                return out
    return []


def fetch_market_day(sess, date_iso):
    """上市＋上櫃全市場單日行情（任一邊失敗不影響另一邊）。"""
    rows = []
    try:
        rows += fetch_twse_market_day(sess, date_iso)
    except Exception:
        pass
    time.sleep(TWSE_SLEEP)
    try:
        rows += fetch_tpex_market_day(sess, date_iso)
    except Exception:
        pass
    return rows


# ============================================================
#  ② 逐檔單月日線（取代 FinMind TaiwanStockPrice 逐檔查詢）
#     — 新上市/新上櫃個股的深度回補用；一個請求拿一整個月
# ============================================================
TWSE_STOCK_DAY_URLS = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY",
    "https://www.twse.com.tw/exchangeReport/STOCK_DAY",
)
TPEX_STOCK_MONTH_URLS = (
    "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock",
    "https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php",
)


def fetch_twse_stock_month(sess, sid, year, month):
    """上市個股單月日線 → [{stock_id,date,open,high,low,close,volume,amount}]。"""
    ymd = f"{year:04d}{month:02d}01"
    for url in TWSE_STOCK_DAY_URLS:
        j = get_json(sess, url, {"date": ymd, "stockNo": sid, "response": "json"})
        if not j:
            continue
        if str(j.get("stat", "OK")) not in ("OK", ""):     # 無資料/尚未上市
            return []
        for fields, rows in _iter_tables(j):
            i_d = _col(fields, "日期")
            i_c = _col(fields, "收盤價")
            if i_d is None or i_c is None:
                continue
            i_o, i_h, i_l = _col(fields, "開盤價"), _col(fields, "最高價"), _col(fields, "最低價")
            i_v, i_a = _col(fields, "成交股數"), _col(fields, "成交金額")
            out = []
            for r in rows:
                iso = roc_to_iso(_cell(r, i_d))
                close = num(_cell(r, i_c))
                if not iso or close is None or close <= 0:
                    continue
                out.append({"stock_id": sid, "date": iso,
                            "open": num(_cell(r, i_o)), "high": num(_cell(r, i_h)),
                            "low": num(_cell(r, i_l)), "close": close,
                            "volume": num(_cell(r, i_v)), "amount": num(_cell(r, i_a))})
            if out:
                return out
    return []


# 舊版 TPEx 個股月線 aaData 欄位順序：日期,成交仟股,成交仟元,開盤,最高,最低,收盤,漲跌,筆數
_TPEX_ST43_POS = {"date": 0, "volume": 1, "amount": 2, "open": 3,
                  "high": 4, "low": 5, "close": 6}


def fetch_tpex_stock_month(sess, sid, year, month):
    """上櫃個股單月日線 → 格式同 fetch_twse_stock_month。"""
    roc = f"{year - 1911:03d}/{month:02d}"
    for url in TPEX_STOCK_MONTH_URLS:
        if "st43_result" in url:
            params = {"l": "zh-tw", "d": roc, "stkno": sid, "o": "json"}
        else:
            params = {"code": sid, "date": f"{year:04d}/{month:02d}/01",
                      "id": "", "response": "json"}
        j = get_json(sess, url, params)
        if not j:
            continue
        for fields, rows in _iter_tables(j):
            if not rows:
                continue
            if fields and _col(fields, "收盤") is not None:
                idx = {"date": _col(fields, "日期"), "open": _col(fields, "開盤"),
                       "high": _col(fields, "最高"), "low": _col(fields, "最低"),
                       "close": _col(fields, "收盤"),
                       "volume": _col(fields, "成交股數", "成交仟股", "成交量"),
                       "amount": _col(fields, "成交金額", "成交仟元", "成交值")}
                vol_k = "仟" in str(fields[idx["volume"]]) if idx["volume"] is not None else False
                amt_k = "仟" in str(fields[idx["amount"]]) if idx["amount"] is not None else False
            else:
                idx, vol_k, amt_k = _TPEX_ST43_POS, True, True
            out = []
            for r in rows:
                iso = roc_to_iso(_cell(r, idx.get("date")))
                close = num(_cell(r, idx.get("close")))
                if not iso or close is None or close <= 0:
                    continue
                vol = num(_cell(r, idx.get("volume")))
                amt = num(_cell(r, idx.get("amount")))
                out.append({"stock_id": sid, "date": iso,
                            "open": num(_cell(r, idx.get("open"))),
                            "high": num(_cell(r, idx.get("high"))),
                            "low": num(_cell(r, idx.get("low"))), "close": close,
                            "volume": (vol * 1000 if (vol is not None and vol_k) else vol),
                            "amount": (amt * 1000 if (amt is not None and amt_k) else amt)})
            if out:
                return out
    return []


def _month_range(start_iso, end_iso):
    y, m = int(start_iso[:4]), int(start_iso[5:7])
    ey, em = int(end_iso[:4]), int(end_iso[5:7])
    while (y, m) <= (ey, em):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


EMPTY_MONTH_STOP = 6        # 連續幾個月無資料就視為「尚未上市」，停止再往回抓


def fetch_stock_history(sess, sid, market, start_iso, end_iso, max_months=0, stats=None):
    """逐檔歷史日線（官方、免費）。market 為 '上櫃' 走 TPEx，其餘走 TWSE；
    兩邊皆試（部分個股會轉板）。max_months>0 時只抓最新的 N 個月（分批回補用）。
    stats 若給定 dict，會回填 {'early_stop': 是否因連續無資料而提前結束}，
    呼叫端可據此判斷「已抓到上市前」而不必再往回排程。"""
    months = list(_month_range(start_iso, end_iso))
    if max_months > 0:
        months = months[-max_months:]
    rows, empty_streak, early_stop = [], 0, False
    for y, m in reversed(months):                    # 由新往舊：新上市股很快就會連續無資料
        got = []
        try:
            if market == "上櫃":
                got = fetch_tpex_stock_month(sess, sid, y, m)
                time.sleep(TPEX_SLEEP)
                if not got:
                    got = fetch_twse_stock_month(sess, sid, y, m)
                    time.sleep(TWSE_SLEEP)
            else:
                got = fetch_twse_stock_month(sess, sid, y, m)
                time.sleep(TWSE_SLEEP)
                if not got:
                    got = fetch_tpex_stock_month(sess, sid, y, m)
                    time.sleep(TPEX_SLEEP)
        except Exception:
            got = []
        rows += [r for r in got if start_iso <= r["date"] <= end_iso]
        empty_streak = 0 if got else empty_streak + 1
        if empty_streak >= EMPTY_MONTH_STOP:
            early_stop = True
            break
    if stats is not None:
        stats["early_stop"] = early_stop
    return rows


# ============================================================
#  ③ 三大法人買賣超（取代 FinMind TaiwanStockInstitutionalInvestorsBuySell）
#     — 上市 T86 / 上櫃 TPEx 日報，皆為「全市場單日」：一天 2 個請求即可涵蓋全市場，
#       比 FinMind 逐檔查詢快上百倍，也不受免費版逐檔限流影響。
# ============================================================
TWSE_T86_URLS = (
    "https://www.twse.com.tw/rwd/zh/fund/T86",
    "https://www.twse.com.tw/fund/T86",
)
TPEX_INST_URLS = (
    "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
    "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php",
)


def _lots(v):
    """股數 → 張（1 張 = 1000 股），保留 1 位小數；None 原樣回傳。"""
    x = num(v)
    return None if x is None else round(x / 1000.0, 1)


def fetch_twse_inst_day(sess, date_iso):
    """上市三大法人買賣超（T86，全市場單日）。
    回傳 [(stock_id, date, 外資張, 投信張, 自營張, 三大法人合計張)]；休市/失敗回 []。"""
    ymd = iso_to_ymd(date_iso)
    for url in TWSE_T86_URLS:
        j = get_json(sess, url, {"date": ymd, "selectType": "ALL", "response": "json"})
        if not j:
            continue
        for fields, rows in _iter_tables(j):
            i_code = _col(fields, "證券代號")
            i_trust = _col(fields, "投信買賣超股數")
            if i_code is None or i_trust is None:
                continue
            i_fmain = _col(fields, "外陸資買賣超股數(不含外資自營商)",
                           "外資及陸資買賣超股數(不含外資自營商)")
            i_fdeal = _col(fields, "外資自營商買賣超股數")
            i_deal = _col(fields, "自營商買賣超股數")
            i_total = _col(fields, "三大法人買賣超股數")
            out = []
            for r in rows:
                sid = _clean_code(_cell(r, i_code))
                if not sid:
                    continue
                fm, fd = _lots(_cell(r, i_fmain)), _lots(_cell(r, i_fdeal))
                foreign = None if (fm is None and fd is None) else round((fm or 0) + (fd or 0), 1)
                out.append((sid, date_iso, foreign, _lots(_cell(r, i_trust)),
                            _lots(_cell(r, i_deal)), _lots(_cell(r, i_total))))
            if out:
                return out
    return []


# 舊版 TPEx 三大法人日報 aaData 欄位順序（無欄名時的退路）
_TPEX_INST_POS = {"code": 0, "foreign": 10, "trust": 13, "dealer": 22, "total": 23}


def fetch_tpex_inst_day(sess, date_iso):
    """上櫃三大法人買賣超（全市場單日）。回傳格式同 fetch_twse_inst_day。"""
    for url in TPEX_INST_URLS:
        if "3itrade_hedge" in url:
            params = {"l": "zh-tw", "se": "EW", "t": "D",
                      "d": iso_to_roc_slash(date_iso), "o": "json"}
        else:
            params = {"type": "Daily", "sect": "EW", "date": date_iso.replace("-", "/"),
                      "id": "", "response": "json"}
        j = get_json(sess, url, params)
        if not j:
            continue
        for fields, rows in _iter_tables(j):
            if not rows:
                continue
            if fields and _col(fields, "投信買賣超股數", "投信買賣超") is not None:
                idx = {"code": _col(fields, "代號", "證券代號", "股票代號"),
                       # 『外資及陸資買賣超股數』已含外資自營商，與 T86 的 foreign 定義一致
                       "foreign": _col(fields, "外資及陸資買賣超股數", "外陸資買賣超股數"),
                       "trust": _col(fields, "投信買賣超股數", "投信買賣超"),
                       "dealer": _col(fields, "自營商買賣超股數", "自營商買賣超"),
                       "total": _col(fields, "三大法人買賣超股數", "三大法人買賣超")}
            else:
                idx = _TPEX_INST_POS
            out = []
            for r in rows:
                sid = _clean_code(_cell(r, idx.get("code")))
                if not sid:
                    continue
                f = _lots(_cell(r, idx.get("foreign")))
                t = _lots(_cell(r, idx.get("trust")))
                d = _lots(_cell(r, idx.get("dealer")))
                tot = _lots(_cell(r, idx.get("total")))
                if tot is None and not (f is None and t is None and d is None):
                    tot = round((f or 0) + (t or 0) + (d or 0), 1)
                out.append((sid, date_iso, f, t, d, tot))
            if out:
                return out
    return []


def fetch_inst_day(sess, date_iso):
    """上市＋上櫃三大法人買賣超（全市場單日）。回傳 (rows, ok_markets)；
    ok_markets 為本次成功取得的市場集合，供呼叫端記錄「該日該市場已抓過」。"""
    rows, ok = [], set()
    try:
        r1 = fetch_twse_inst_day(sess, date_iso)
        if r1:
            rows += r1
            ok.add("上市")
    except Exception:
        pass
    time.sleep(TWSE_SLEEP)
    try:
        r2 = fetch_tpex_inst_day(sess, date_iso)
        if r2:
            rows += r2
            ok.add("上櫃")
    except Exception:
        pass
    return rows, ok


# ============================================================
#  ④ 集保股權分散（取代 FinMind TaiwanStockHoldingSharesPer）
#     — 集保結算所 TDCC 開放資料：免費、免 token、一次拿全市場最新一週。
#       持股分級 12~15（400,001 股以上）加總 = 「400 張大戶持股%」。
#       合計級距（16 / 'total'）的股數 = 集保總股數，用來算發行/流通張數。
# ============================================================
TDCC_URLS = (
    "https://opendata.tdcc.com.tw/getOD.aspx?id=1-5",
    "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5",
)
BIG400_LEVELS = {"12", "13", "14", "15"}      # 400,001~600,000 / ~800,000 / ~1,000,000 / 以上


def is_big400_level(level):
    """持股分級是否屬『400 張(=400,000 股)以上大戶』（下界 ≥ 400,001 股）。
    相容兩種標記：純級距索引(1~15，其中 12~15 為大戶級距) 與範圍字串
    (如 '400,001-600,000'、'1,000,001以上')，TDCC 若改版換寫法也不會誤判。"""
    s = re.sub(r"\s", "", str(level or ""))
    if not s:
        return False
    if re.fullmatch(r"\d{1,2}", s):
        return s.lstrip("0") in BIG400_LEVELS or s in BIG400_LEVELS
    if "合計" in s or "total" in s.lower() or "差異" in s:
        return False
    m = re.search(r"([\d,]+)", s)
    if not m:
        return False
    try:
        return int(m.group(1).replace(",", "")) >= 400001
    except ValueError:
        return False


def is_total_level(level):
    """是否為『合計』級距（集保總股數的那一列）；差異數調整列不算。"""
    s = re.sub(r"\s", "", str(level or ""))
    if "差異" in s:
        return False
    return s == "16" or "合計" in s or "total" in s.lower()


def fetch_tdcc_shareholding(sess):
    """集保戶股權分散表（全市場，最新可得週次；少數版本會一次給多週）。
    回傳 {date_iso: {sid: {'big400': 百分比, 'total_shares': 集保總股數}}}；失敗回 {}。"""
    text = None
    for url in TDCC_URLS:
        text = get_text(sess, url, timeout=120, encoding="utf-8-sig")
        if text and "," in text:
            break
        text = None
    if not text:
        return {}
    try:
        rdr = csv.DictReader(io.StringIO(text.lstrip("﻿")))
        rows = list(rdr)
    except Exception:
        return {}
    if not rows:
        return {}

    def key_of(row, *names):
        for k in row.keys():
            kk = re.sub(r"\s", "", str(k))
            if any(n in kk for n in names):
                return k
        return None

    k_date = key_of(rows[0], "資料日期", "date")
    k_sid = key_of(rows[0], "證券代號", "stock_id")
    k_lvl = key_of(rows[0], "持股分級", "level")
    k_shr = key_of(rows[0], "股數", "shares")
    k_pct = key_of(rows[0], "比例", "percent")
    if not (k_date and k_sid and k_lvl):
        return {}

    out = {}
    for r in rows:
        iso = roc_to_iso(r.get(k_date))
        sid = _clean_code(r.get(k_sid))
        if not iso or not sid:
            continue
        lvl = r.get(k_lvl)
        rec = out.setdefault(iso, {}).setdefault(sid, {"big400": 0.0, "total_shares": 0.0,
                                                       "_hit": False})
        if is_big400_level(lvl):
            p = num(r.get(k_pct)) if k_pct else None
            if p is not None:
                rec["big400"] += p
                rec["_hit"] = True
        elif is_total_level(lvl):
            s = num(r.get(k_shr)) if k_shr else None
            if s:
                rec["total_shares"] = max(rec["total_shares"], s)
    for iso in out:
        for sid, rec in out[iso].items():
            rec["big400"] = round(rec["big400"], 2) if rec.pop("_hit", False) else None
    return out


# ============================================================
#  ⑤ 公司基本資料：產業別／股名／市場別／發行股數
#     （取代 FinMind TaiwanStockInfo 與 TaiwanStockShareholding）
# ============================================================
TWSE_T187AP03 = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_T187AP03 = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
ISIN_URLS = (("https://isin.twse.com.tw/isin/C_public.jsp?strMode=2", "上市"),
             ("https://isin.twse.com.tw/isin/C_public.jsp?strMode=4", "上櫃"))

# 上市/上櫃『產業別』代碼 → 名稱（t187ap03 部分版本回傳代碼而非名稱）
INDUSTRY_CODE = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "07": "化學生技醫療", "08": "玻璃陶瓷",
    "09": "造紙工業", "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業",
    "13": "電子工業", "14": "建材營造", "15": "航運業", "16": "觀光餐旅",
    "17": "金融保險", "18": "貿易百貨", "19": "綜合", "20": "其他",
    "21": "化學工業", "22": "生技醫療業", "23": "油電燃氣業", "24": "半導體業",
    "25": "電腦及週邊設備業", "26": "光電業", "27": "通信網路業", "28": "電子零組件業",
    "29": "電子通路業", "30": "資訊服務業", "31": "其他電子業", "32": "文化創意業",
    "33": "農業科技業", "34": "電子商務", "35": "綠能環保", "36": "數位雲端",
    "37": "運動休閒", "38": "居家生活",
}


def _norm_industry(v):
    s = re.sub(r"\s", "", str(v or ""))
    if not s:
        return ""
    if re.fullmatch(r"\d{1,2}", s):
        return INDUSTRY_CODE.get(s.zfill(2), "")
    return s


def _meta_from_openapi(sess, url, market):
    """t187ap03（公司基本資料）→ {sid: {...}}。欄名採自適應比對，版本改版也不易壞。"""
    j = get_json(sess, url, timeout=60)
    if not isinstance(j, list) or not j:
        return {}
    keys = list(j[0].keys())

    def kk(*names):
        for n in names:
            for k in keys:
                if re.sub(r"\s", "", str(k)) == n:
                    return k
        for n in names:
            for k in keys:
                if n in re.sub(r"\s", "", str(k)):
                    return k
        return None

    k_sid = kk("公司代號", "SecuritiesCompanyCode", "Code", "出表日期公司代號")
    k_name = kk("公司簡稱", "CompanyAbbreviation", "公司名稱", "CompanyName")
    k_ind = kk("產業別", "SecuritiesIndustryCode", "IndustryCode", "Industry")
    k_shr = kk("已發行普通股數或TDR原發行股數", "已發行普通股數", "NumberOfSharesIssued")
    k_cap = kk("實收資本額", "PaidInCapital")
    k_par = kk("普通股每股面額", "ParValue")
    if not k_sid:
        return {}
    out = {}
    for it in j:
        sid = _clean_code(it.get(k_sid))
        if not sid:
            continue
        shares = num(it.get(k_shr)) if k_shr else None
        if not shares and k_cap:                 # 無發行股數欄位時：實收資本額 ÷ 每股面額
            cap = num(it.get(k_cap))
            par = num(re.sub(r"[^\d.]", "", str(it.get(k_par) or ""))) if k_par else None
            if cap and par and par > 0:
                shares = cap / par
        out[sid] = {"name": re.sub(r"\s", "", str(it.get(k_name) or "")) if k_name else "",
                    "market": market,
                    "industry": _norm_industry(it.get(k_ind)) if k_ind else "",
                    "issued_shares": shares}
    return out


_ISIN_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_ISIN_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)


def _meta_from_isin(sess, url, market):
    """證交所 ISIN 服務（HTML）：代號/名稱/市場別/產業別。t187ap03 失效時的退路。"""
    html = get_text(sess, url, timeout=60, encoding="big5-hkscs")
    if not html:
        return {}
    out = {}
    for tr in _ISIN_ROW.findall(html):
        tds = [re.sub(r"<[^>]*>|&nbsp;", " ", t).strip() for t in _ISIN_TD.findall(tr)]
        if len(tds) < 6:
            continue
        parts = re.split(r"[\s　]+", tds[0].strip(), maxsplit=1)
        sid = _clean_code(parts[0])
        if not sid:
            continue
        out[sid] = {"name": parts[1].strip() if len(parts) > 1 else "",
                    "market": market, "industry": _norm_industry(tds[4]),
                    "issued_shares": None}
    return out


def fetch_company_meta(sess):
    """全市場公司基本資料 → {sid: {'name','market','industry','issued_shares'}}。
    先試 TWSE/TPEx 的 t187ap03 OpenAPI，缺漏再用 ISIN 服務補（兩者皆免費免 token）。"""
    meta = {}
    for url, mkt in ((TWSE_T187AP03, "上市"), (TPEX_T187AP03, "上櫃")):
        try:
            meta.update(_meta_from_openapi(sess, url, mkt))
        except Exception:
            pass
    if len(meta) < 800:                          # OpenAPI 失效/大量缺漏 → 用 ISIN 補底
        for url, mkt in ISIN_URLS:
            try:
                for sid, rec in _meta_from_isin(sess, url, mkt).items():
                    cur = meta.setdefault(sid, rec)
                    for f in ("name", "industry"):
                        if not cur.get(f) and rec.get(f):
                            cur[f] = rec[f]
                    cur.setdefault("market", rec["market"])
            except Exception:
                pass
    return meta


# ============================================================
#  ⑥ 加權指數 TAIEX（取代 FinMind TaiwanVariousIndicators5Seconds）
#     — 證交所『發行量加權股價指數歷史資料』：一次一個月的日 OHLC，免費免 token。
# ============================================================
TAIEX_HIST_URLS = (
    "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST",
    "https://www.twse.com.tw/exchangeReport/MI_5MINS_HIST",
)


def fetch_taiex_month(sess, year, month):
    """加權指數單月日線 → [(date_iso, open, high, low, close)]；失敗回 []。"""
    ymd = f"{year:04d}{month:02d}01"
    for url in TAIEX_HIST_URLS:
        j = get_json(sess, url, {"date": ymd, "response": "json"})
        if not j:
            continue
        for fields, rows in _iter_tables(j):
            i_d = _col(fields, "日期")
            i_c = _col(fields, "收盤指數")
            if i_d is None or i_c is None:
                continue
            i_o, i_h, i_l = (_col(fields, "開盤指數"), _col(fields, "最高指數"),
                             _col(fields, "最低指數"))
            out = []
            for r in rows:
                iso = roc_to_iso(_cell(r, i_d))
                c = num(_cell(r, i_c))
                if not iso or c is None:
                    continue
                out.append((iso, num(_cell(r, i_o)), num(_cell(r, i_h)),
                            num(_cell(r, i_l)), c))
            if out:
                return out
    return []


def fetch_taiex_series(sess, months=3):
    """最近 N 個月的加權指數日 OHLC，回傳 (dates, highs, closes)（供回撤計算）；無資料回 None。"""
    today = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)     # 台北時間
    y, m = today.year, today.month
    rows = []
    for _ in range(max(1, months)):
        try:
            rows += fetch_taiex_month(sess, y, m)
        except Exception:
            pass
        time.sleep(TWSE_SLEEP)
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    if not rows:
        return None
    by = {}
    for iso, _o, h, l, c in rows:
        by[iso] = (h if h is not None else c, c)
    ds = sorted(by)
    return ds, [by[d][0] for d in ds], [by[d][1] for d in ds]
