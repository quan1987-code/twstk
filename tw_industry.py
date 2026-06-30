# -*- coding: utf-8 -*-
r"""
台股產業鏈分類（tw_industry.py）
================================================================
為每一檔股票提供「產業類型」標籤，給所有頁面在股名下方標註用。

兩層來源：
  1) CURATED：手工維護的『產業鏈上中下游＋子類』對照表（如「電子上游-被動元件」），
     最貼近看盤習慣，但僅涵蓋主要個股。
  2) FinMind TaiwanStockInfo 的『大分類 industry_category』（如「半導體業」「電子零組件業」），
     作為其餘個股的補底；抓一次快取進 twstock.db 的 industry 表，之後免重抓。

對外：
  fetch_finmind_industry(con, token)  # 在有 token 的步驟(screener)呼叫，補底快取
  label_map(con)                      # 回傳 {sid: 產業標籤}（curated 優先），給 build_site / disposition
"""
import sqlite3

try:
    import requests
except Exception:
    requests = None

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

# ===== 手工『產業鏈上中下游＋子類』對照表（主要個股；其餘以 FinMind 大分類補底） =====
CURATED = {
    # ---- 電子上游：晶圓代工 / 磊晶 ----
    "2330": "電子上游-晶圓代工", "2303": "電子上游-晶圓代工", "5347": "電子上游-晶圓代工",
    "6770": "電子上游-晶圓代工", "3105": "電子上游-磊晶",
    # ---- 電子上游：IC 設計 / 矽智財 ----
    "2454": "電子上游-IC設計", "2379": "電子上游-IC設計", "2458": "電子上游-IC設計",
    "3034": "電子上游-IC設計", "8016": "電子上游-IC設計", "3014": "電子上游-IC設計",
    "3035": "電子上游-IC設計", "2436": "電子上游-IC設計", "8086": "電子上游-IC設計",
    "6531": "電子上游-IC設計", "5269": "電子上游-IC設計", "6415": "電子上游-IC設計",
    "4966": "電子上游-IC設計", "6526": "電子上游-IC設計", "3443": "電子上游-IC設計",
    "3661": "電子上游-IC設計", "2401": "電子上游-IC設計", "6202": "電子上游-IC設計",
    "8049": "電子上游-IC設計", "3556": "電子上游-IC設計", "5471": "電子上游-IC設計",
    "3529": "電子上游-矽智財", "6643": "電子上游-矽智財",
    # ---- 電子上游：記憶體 ----
    "2408": "電子上游-記憶體", "2344": "電子上游-記憶體", "2337": "電子上游-記憶體",
    "8299": "電子上游-記憶體", "4967": "電子上游-記憶體",
    # ---- 電子上游：被動元件 ----
    "2327": "電子上游-被動元件", "2492": "電子上游-被動元件", "3026": "電子上游-被動元件",
    "6173": "電子上游-被動元件", "2375": "電子上游-被動元件", "5314": "電子上游-被動元件",
    "3090": "電子上游-被動元件",
    # ---- 電子中游：封測 / IC 通路 ----
    "3711": "電子中游-封裝測試", "2449": "電子中游-封裝測試", "2441": "電子中游-封裝測試",
    "6239": "電子中游-封裝測試", "8150": "電子中游-封裝測試", "3686": "電子中游-封裝測試",
    "2329": "電子中游-封裝測試", "5263": "電子中游-封裝測試",
    "3702": "電子中游-IC通路", "3036": "電子中游-IC通路", "2347": "電子中游-IC通路",
    "8112": "電子中游-IC通路",
    # ---- 電子中游：PCB / 銅箔基板 ----
    "3037": "電子中游-PCB", "3044": "電子中游-PCB", "4958": "電子中游-PCB",
    "8046": "電子中游-PCB", "2316": "電子中游-PCB", "6213": "電子中游-PCB",
    "6191": "電子中游-PCB", "3189": "電子中游-PCB", "2368": "電子中游-PCB",
    "6269": "電子中游-PCB", "5469": "電子中游-PCB", "8358": "電子中游-PCB",
    "6552": "電子中游-PCB", "2383": "電子中游-銅箔基板",
    # ---- 電子中游：面板 / 光學 / 散熱 / 連接器 / 機殼 ----
    "2409": "電子中游-面板", "3481": "電子中游-面板", "6116": "電子中游-面板",
    "3008": "電子中游-光學鏡頭", "3406": "電子中游-光學鏡頭", "3019": "電子中游-光學鏡頭",
    "3017": "電子中游-散熱", "3324": "電子中游-散熱", "6230": "電子中游-散熱",
    "3653": "電子中游-散熱",
    "2354": "電子中游-連接器", "3533": "電子中游-連接器", "2392": "電子中游-連接器",
    "6151": "電子中游-連接器", "2474": "電子中游-連接器",
    "5009": "電子中游-機殼",
    # ---- 電子中游：網通 / 光通訊 ----
    "2345": "電子中游-網通", "5388": "電子中游-網通", "6285": "電子中游-網通",
    "3704": "電子中游-網通", "4906": "電子中游-網通", "3380": "電子中游-網通",
    "2419": "電子中游-網通", "2314": "電子中游-網通",
    "4979": "電子中游-光通訊", "4977": "電子中游-光通訊", "6464": "電子中游-光通訊",
    "4908": "電子中游-光通訊",
    # ---- 電子下游：系統組裝 / 伺服器 / EMS / 工業電腦 ----
    "2317": "電子下游-系統組裝", "2356": "電子下游-系統組裝", "2324": "電子下游-系統組裝",
    "2376": "電子下游-系統組裝", "2377": "電子下游-系統組裝", "2353": "電子下游-系統組裝",
    "2475": "電子下游-系統組裝", "2382": "電子下游-伺服器", "3231": "電子下游-伺服器",
    "6669": "電子下游-伺服器", "4938": "電子下游-EMS",
    "2395": "電子下游-工業電腦", "6414": "電子下游-工業電腦", "2331": "電子下游-工業電腦",
    # ---- 半導體設備 / 矽晶圓 ----
    "3680": "半導體-設備", "6196": "半導體-設備", "3131": "半導體-設備",
    "6187": "半導體-設備", "6510": "半導體-設備", "6271": "半導體-設備",
    "5483": "半導體-矽晶圓",
    # ---- 電源 / 電機重電 ----
    "2308": "電子-電源供應", "1519": "電機-重電", "1513": "電機-重電",
    "1503": "電機-重電", "1504": "電機-重電", "1535": "電機-重電",
    # ---- 金融 ----
    "2880": "金融-金控", "2881": "金融-金控", "2882": "金融-金控", "2883": "金融-金控",
    "2884": "金融-金控", "2885": "金融-金控", "2886": "金融-金控", "2887": "金融-金控",
    "2888": "金融-金控", "2889": "金融-金控", "2890": "金融-金控", "2891": "金融-金控",
    "2892": "金融-金控", "5880": "金融-金控", "2812": "金融-銀行",
    # ---- 鋼鐵 / 塑化 / 水泥 ----
    "2002": "鋼鐵", "2006": "鋼鐵", "2007": "鋼鐵", "2014": "鋼鐵", "2015": "鋼鐵",
    "2027": "鋼鐵-不鏽鋼",
    "1301": "塑化", "1303": "塑化", "1326": "塑化", "6505": "塑化", "1314": "塑化",
    "1101": "水泥", "1102": "水泥", "1103": "水泥",
    # ---- 航運 / 航空 ----
    "2603": "航運-貨櫃", "2609": "航運-貨櫃", "2615": "航運-貨櫃",
    "2606": "航運-散裝", "2607": "航運-散裝", "2610": "航空", "2618": "航空",
    # ---- 食品 / 紡織 / 汽車 / 橡膠 / 觀光 / 營建 / 電信 / 生技 / 百貨 ----
    "1216": "食品", "1210": "食品", "1227": "食品",
    "1402": "紡織", "1434": "紡織", "1476": "紡織",
    "2207": "汽車-整車", "2201": "汽車-整車", "2105": "橡膠輪胎",
    "2727": "觀光餐飲", "2729": "觀光餐飲",
    "9945": "營建", "2542": "營建", "2548": "營建",
    "2412": "電信", "3045": "電信", "4904": "電信",
    "4137": "生技-製藥", "1701": "生技-製藥", "6446": "生技-製藥", "1565": "生技-醫材",
    "2912": "貿易百貨",
}
CURATED = {k: v for k, v in CURATED.items() if v and len(k) == 4 and k.isdigit()}


def ensure_table(con):
    con.execute("CREATE TABLE IF NOT EXISTS industry(stock_id TEXT PRIMARY KEY, category TEXT)")
    con.commit()


_TYPE_MKT = {"twse": "上市", "tpex": "上櫃", "上市": "上市", "上櫃": "上櫃",
             "TWSE": "上市", "TPEX": "上櫃"}


def fetch_finmind_industry(con, token, force=False, min_have=200):
    """抓 FinMind TaiwanStockInfo（每次跑一次 1 call）：
    1) industry_category 寫進 industry 表(產業大分類補底)。
    2) stock_name/market 補進 stock 表 —— 解決部分清單『只有代號、沒有股名』。
    回傳本次處理的個股數。"""
    ensure_table(con)
    con.execute("CREATE TABLE IF NOT EXISTS stock(stock_id TEXT PRIMARY KEY, name TEXT, market TEXT)")
    if requests is None or not token:
        return 0
    try:
        r = requests.get(FINMIND_URL, headers={"Authorization": f"Bearer {token}"},
                         params={"dataset": "TaiwanStockInfo"}, timeout=60)
        if r.status_code != 200:
            return 0
        data = r.json().get("data", [])
    except Exception:
        return 0
    seen = {}
    for it in data:
        sid = str(it.get("stock_id", "")).strip()
        if len(sid) == 4 and sid.isdigit() and sid not in seen:
            seen[sid] = (str(it.get("industry_category", "")).strip(),
                         str(it.get("stock_name", "")).strip(),
                         str(it.get("type", "")).strip())
    for sid, (cat, nm, typ) in seen.items():
        con.execute("INSERT INTO industry(stock_id,category) VALUES(?,?) "
                    "ON CONFLICT(stock_id) DO UPDATE SET category=excluded.category", (sid, cat))
        if nm:   # 補股名（不覆寫既有市場別，只在缺漏時補）
            con.execute("INSERT INTO stock(stock_id,name) VALUES(?,?) "
                        "ON CONFLICT(stock_id) DO UPDATE SET name=excluded.name", (sid, nm))
            m = _TYPE_MKT.get(typ)
            if m:
                con.execute("UPDATE stock SET market=? WHERE stock_id=? AND (market IS NULL OR market='')",
                            (m, sid))
    con.commit()
    return len(seen)


# FinMind 偶有英文值，做個輕量正規化（對不到就原樣顯示）
_CAT_FIX = {
    "Semiconductor": "半導體業", "Other Electronic": "電子零組件業",
    "Optoelectronic": "光電業", "Communications Technology": "通信網路業",
    "Computer and Peripheral Equipment": "電腦及週邊設備業",
    "Electronic Parts/Components": "電子零組件業",
}


def _norm_cat(cat):
    return _CAT_FIX.get(cat, cat) if cat else ""


def label_map(con):
    """回傳 {sid: 產業標籤}。curated 優先，否則用 FinMind 大分類快取。無資料者不列。"""
    cat = {}
    try:
        for sid, c in con.execute("SELECT stock_id, category FROM industry"):
            if c:
                cat[sid] = _norm_cat(c)
    except sqlite3.Error:
        pass
    out = dict(cat)
    out.update(CURATED)   # curated 覆寫大分類
    return {k: v for k, v in out.items() if v}
