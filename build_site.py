# -*- coding: utf-8 -*-
r"""
雲端網頁版看板（雙分頁・手機觸控・PWA）
================================================================
分頁一（首頁）：市場回撤
  顯示「台股加權指數 / 美股費城半導體 SOX / 台積電 2330」三張卡片，
  每張含：歷史最高收盤(附日期)、最近收盤(附日期)、距高點回撤%。
  資料來源：yfinance（Yahoo，完整歷史；台積電抓不到時改用本地 twstock.db）。

分頁二（月均量爆量起漲）：選股看板
  統計卡 + 量比排行 + 可排序/搜尋表格 + 點名稱看K線技術圖。
  另含「指標說明」面板，解釋 月均量 / 量比 / 5日量÷月量 / 季線乖離 / 評分 等。

輸出 site/index.html + site/manifest.json（給 GitHub Pages）。
需要套件：pandas、yfinance（首頁市場資料用；沒裝也能跑，首頁顯示「資料暫時無法取得」）。
"""
import os
import sys
import time
import glob
import json
import math
import sqlite3
import datetime
import pandas as pd

try:
    import yfinance as yf
except Exception:
    yf = None

try:
    import requests
except Exception:
    requests = None

try:
    import tw_industry
except Exception:
    tw_industry = None

try:
    import tw_concepts
except Exception:
    tw_concepts = None

DB_PATH = "twstock.db"
LOOKBACK_BARS = 1000
TRUST_CHART_BARS = 320   # 投信候選嵌入較短歷史以控制檔案大小
OUT_DIR = "site"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")
TWII_FRESH_LOOKBACK_DAYS = 7   # 用 FinMind 補「最近幾天」加權指數新鮮度（非全歷史，控制配額）

# 首頁三標的：(代碼, yfinance符號, 顯示名, 數值型態 'index'整數 / 'price'兩位小數)
MARKET_TARGETS = [
    ("TWII", "^TWII", "台股加權指數", "index"),
    ("SOX",  "^SOX",  "費城半導體 SOX", "index"),
    ("KOSPI", "^KS11", "韓國 KOSPI", "index"),
    ("TSMC", "2330.TW", "台積電 2330", "price"),
]

# Yahoo 代號 → Stooq 代號（雙來源合併用；Stooq 免金鑰 CSV，d1 設極早日期取全歷史）。
# Yahoo 對 ^TWII／^KS11 這類非美股指數，批次/單檔歷史抓取常「有回傳但少最後一天」──
# 不是完全失敗，而是悄悄回傳到前一交易日就停了，光靠 try/except 抓不到這種情況。
STOOQ_SYM = {"^TWII": "^twse", "^SOX": "^sox", "^KS11": "^kospi", "2330.TW": "2330.tw"}


def find_latest_csv():
    cands = glob.glob(os.path.join("output", "breakout_*.csv")) or glob.glob("breakout_*.csv")
    return max(cands, key=os.path.getmtime) if cands else None


def find_latest_trust():
    cands = glob.glob(os.path.join("output", "trust_*.json")) or glob.glob("trust_*.json")
    return max(cands, key=os.path.getmtime) if cands else None


def find_latest_extras():
    cands = glob.glob(os.path.join("output", "extras_*.json")) or glob.glob("extras_*.json")
    return max(cands, key=os.path.getmtime) if cands else None


def load_extras():
    p = find_latest_extras()
    if not p:
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def find_latest_flows():
    cands = glob.glob(os.path.join("output", "flows_*.json")) or glob.glob("flows_*.json")
    return max(cands, key=os.path.getmtime) if cands else None


def load_flows():
    p = find_latest_flows()
    if not p:
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_trust():
    tp = find_latest_trust()
    if not tp:
        return {}
    try:
        with open(tp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_history(stock_ids, db_path, limit=LOOKBACK_BARS):
    hist = {}
    if not os.path.exists(db_path):
        return hist, False
    con = sqlite3.connect(db_path)
    for sid in stock_ids:
        try:
            rows = con.execute(
                "SELECT date,open,high,low,close,volume FROM price "
                "WHERE stock_id=? ORDER BY date DESC LIMIT ?", (sid, limit)).fetchall()
        except sqlite3.Error:
            rows = []
        rows = rows[::-1]
        out = []
        for d, o, h, l, c, v in rows:
            if c is None:
                continue
            out.append([d,
                        round(o, 2) if o is not None else None,
                        round(h, 2) if h is not None else None,
                        round(l, 2) if l is not None else None,
                        round(c, 2),
                        round((v or 0) / 1000.0, 1)])
        if out:
            hist[sid] = out
    con.close()
    return hist, True


def _r2(x):
    return round(x, 2) if x is not None else None


def load_industry(db_path):
    """回傳 {sid: 產業標籤}；無 DB 或模組時回空 dict。"""
    if tw_industry is None or not os.path.exists(db_path):
        return {}
    try:
        con = sqlite3.connect(db_path)
        m = tw_industry.label_map(con)
        con.close()
        return m
    except Exception:
        return {}


def write_stock_data(db_path, out_dir, industry=None):
    """為『每一檔』股票輸出精簡版逐檔資料檔 site/data/{代號}.json（含 2005 以來日線，
    及三大法人/外資/主力買賣超與 400張大戶持股% 之深度歷史、發行張數），
    並輸出 site/data/_index.json（全清單，給首頁搜尋用）。圖表改成『點哪檔才抓哪檔』，HTML 不再內嵌歷史。"""
    if not os.path.exists(db_path):
        return 0
    industry = industry or {}
    ddir = os.path.join(out_dir, "data")
    os.makedirs(ddir, exist_ok=True)
    con = sqlite3.connect(db_path)
    info = {r[0]: (r[1], r[2]) for r in con.execute("SELECT stock_id,name,market FROM stock")}
    # 發行張數（表頭『發行 / 流通張數』用；流通 = 發行 ×(1−400張大戶%) 於網頁端計算）。
    issued = {}
    try:
        for sid, iss in con.execute("SELECT stock_id,issued_lots FROM stockmeta"):
            if iss is not None:
                issued[sid] = iss
    except sqlite3.Error:
        issued = {}
    # 三大法人(投信 trust_lots / 合計「主力」total_lots / 外資 foreign_lots) 與 400張大戶持股%
    # 都可能有多年歷史、資料量大，故『逐檔查詢』（PRIMARY KEY(stock_id,date) 走索引），
    # 不整表預載以省記憶體。has_inst_cols 判斷舊版 DB 是否只有 trust_lots。
    try:
        con.execute("SELECT total_lots,foreign_lots FROM inst LIMIT 1").fetchone()
        has_inst_cols = True
    except sqlite3.Error:
        has_inst_cols = False
    sids = [r[0] for r in con.execute("SELECT DISTINCT stock_id FROM price")]
    index = []
    n = 0
    for sid in sids:
        rows = con.execute("SELECT date,open,high,low,close,volume FROM price "
                           "WHERE stock_id=? ORDER BY date", (sid,)).fetchall()
        d = []; o = []; h = []; l = []; c = []; v = []
        for dd, oo, hh, ll, cc, vv in rows:
            if cc is None:
                continue
            d.append(dd); o.append(_r2(oo)); h.append(_r2(hh)); l.append(_r2(ll))
            c.append(_r2(cc)); v.append(round((vv or 0) / 1000.0, 1))
        if not d:
            continue
        # 逐檔取三大法人與集保大戶（隨個股歷史深度可能達多年；走 (stock_id,date) 索引）
        im = {}; mm = {}; fim = {}
        if has_inst_cols:
            for dd, tt, tot, fr in con.execute(
                    "SELECT date,trust_lots,total_lots,foreign_lots FROM inst "
                    "WHERE stock_id=? ORDER BY date", (sid,)):
                if tt is not None: im[dd] = tt
                if tot is not None: mm[dd] = tot
                if fr is not None: fim[dd] = fr
        else:
            for dd, tt in con.execute(
                    "SELECT date,trust_lots FROM inst WHERE stock_id=? ORDER BY date", (sid,)):
                if tt is not None: im[dd] = tt
        sh = {}
        try:
            for dd, pct in con.execute(
                    "SELECT date,big400_pct FROM shareholding WHERE stock_id=? ORDER BY date", (sid,)):
                if pct is not None: sh[dd] = pct
        except sqlite3.Error:
            sh = {}
        ts = len(d); t = []
        if im:
            imin = min(im.keys())
            lo = 0
            while lo < len(d) and d[lo] < imin:
                lo += 1
            ts = lo
            t = [round(im.get(dd, 0.0), 1) for dd in d[ts:]]
        # 主力買賣超副圖：用三大法人合計，讓每一檔都有（深度歷史回補到約 2019 起）。
        mfs = len(d); mf = []
        if mm:
            mmin = min(mm.keys())
            lo = 0
            while lo < len(d) and d[lo] < mmin:
                lo += 1
            mfs = lo
            mf = [round(mm.get(dd, 0.0), 1) for dd in d[mfs:]]
        # 外資買賣超副圖（與投信同結構：起始索引 fs + 逐日淨買超 f）
        fs = len(d); fser = []
        if fim:
            fmin = min(fim.keys())
            lo = 0
            while lo < len(d) and d[lo] < fmin:
                lo += 1
            fs = lo
            fser = [round(fim.get(dd, 0.0), 1) for dd in d[fs:]]
        # 400張大戶持股%：稀疏 [日期,%] 點（僅週快照；網頁端對每根K棒前向填補）
        b4 = [[dd, round(sh[dd], 2)] for dd in sorted(sh.keys())]
        name, mk = info.get(sid, ("", ""))
        ind = industry.get(sid, "")
        obj = {"n": name, "m": mk, "ind": ind, "d": d, "o": o, "h": h, "l": l, "c": c, "v": v,
               "ts": ts, "t": t, "mfs": mfs, "mf": mf, "fs": fs, "f": fser, "b4": b4,
               "iss": issued.get(sid)}
        with open(os.path.join(ddir, f"{sid}.json"), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        index.append([sid, name, mk, ind]); n += 1
    with open(os.path.join(ddir, "_index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    con.close()
    return n


def month_value_zone(con, sid, months=36):
    """⑨ 伺服器端計算『爆量月價值區間』：最近 months 個月內，成交量最大的那個月份的月K高低，
    看現價落在其中的位置。回傳 {label, cls, pos(%)} 或 None。"""
    rows = con.execute("SELECT date,high,low,close,volume FROM price "
                       "WHERE stock_id=? ORDER BY date", (sid,)).fetchall()
    if len(rows) < 20:
        return None
    magg = {}; order = []
    for dd, hh, ll, cc, vv in rows:
        if cc is None:
            continue
        k = dd[:7]
        if k not in magg:
            magg[k] = [hh, ll, cc, vv or 0]; order.append(k)
        else:
            g = magg[k]
            if hh is not None:
                g[0] = max(g[0], hh) if g[0] is not None else hh
            if ll is not None:
                g[1] = min(g[1], ll) if g[1] is not None else ll
            g[2] = cc; g[3] += (vv or 0)
    if not order:
        return None
    recent = order[-months:] if len(order) > months else order
    mx = None
    for k in recent:
        if mx is None or magg[k][3] > magg[mx][3]:
            mx = k
    if mx is None:
        return None
    H, L = magg[mx][0], magg[mx][1]
    if not (H and L and H > L):
        return None
    P = rows[-1][3]
    if P is None:
        return None
    mid = (H + L) / 2
    pos = round((P - L) / (H - L) * 100)
    if P > H:
        return {"label": "月量高之上", "cls": "z-above", "pos": pos}
    if L <= P <= mid:
        return {"label": "近爆量低★", "cls": "z-value", "pos": pos}
    if P >= mid:
        return {"label": "爆量月上半", "cls": "z-upper", "pos": pos}
    return {"label": "破爆量低", "cls": "z-below", "pos": pos}


# ---------------- 首頁：市場回撤 ----------------
def _drawdown(dates, highs, closes):
    """dates/highs/closes：由舊到新。歷史最高取『盤中最高價』，最近值取收盤價。"""
    rows = [(d, h, c) for d, h, c in zip(dates, highs, closes)
            if h is not None and h == h and h > 0 and c is not None and c == c and c > 0]
    if not rows:
        return None
    ds = [r[0] for r in rows]
    hs = [r[1] for r in rows]
    cs = [r[2] for r in rows]
    ath = max(hs)
    ai = hs.index(ath)
    last = cs[-1]
    return {"ath": round(ath, 2), "ath_date": ds[ai],
            "last": round(last, 2), "last_date": ds[-1],
            "dd": round((last / ath - 1) * 100, 2)}


def _realized_vol(closes):
    """由日收盤序列(舊→新)算『年化歷史波動率(%)』＝日對數報酬標準差 × √252。
    回傳 {hv20, hv60, pct1y}：hv20/hv60＝近 20/60 交易日年化波動率；
    pct1y＝當前 20 日波動率落在『近一年每日 20 日波動率』的百分位（0=一年最低、100=最高），
    用來判斷「現在波動相對自己近一年是高是低」。資料不足回 None。"""
    cs = [c for c in closes if c is not None and c == c and c > 0]
    if len(cs) < 25:
        return None
    rets = [math.log(cs[i] / cs[i - 1]) for i in range(1, len(cs))]

    def ann(win):
        if len(rets) < win:
            return None
        seg = rets[-win:]
        m = sum(seg) / len(seg)
        var = sum((x - m) ** 2 for x in seg) / (len(seg) - 1)
        return math.sqrt(var * 252.0) * 100.0

    hv20, hv60 = ann(20), ann(60)
    if hv20 is None:
        return None
    pct = None
    if len(rets) >= 40:   # 至少要能算出多個 20 日 HV，百分位才有意義
        hs = []
        for end in range(20, len(rets) + 1):
            seg = rets[end - 20:end]
            m = sum(seg) / len(seg)
            var = sum((x - m) ** 2 for x in seg) / (len(seg) - 1)
            hs.append(math.sqrt(var * 252.0) * 100.0)
        recent = hs[-252:]
        cur = hs[-1]
        pct = round(sum(1 for x in recent if x <= cur) / len(recent) * 100)
    return {"hv20": round(hv20, 1),
            "hv60": round(hv60, 1) if hv60 is not None else None,
            "pct1y": pct}


def fetch_yf_series(symbol):
    """回傳 (dates,highs,closes)，由舊到新；抓不到回 None。"""
    if yf is None:
        return None
    try:
        df = yf.Ticker(symbol).history(period="max", interval="1d", auto_adjust=False)
        if df is None or df.empty or "Close" not in df.columns or "High" not in df.columns:
            return None
        sub = df[["High", "Close"]].dropna()
        if sub.empty:
            return None
        dates = [d.strftime("%Y-%m-%d") for d in sub.index]
        highs = [float(x) for x in sub["High"].values]
        closes = [float(x) for x in sub["Close"].values]
        return dates, highs, closes
    except Exception as e:
        print(f"  yfinance 抓 {symbol} 失敗：{e}")
        return None


def fetch_stooq_series(stooq_sym):
    """Stooq 免金鑰日線 CSV（d1 設極早日期盡量取全歷史）。抓不到／限流／空頁回 None。"""
    if requests is None or not stooq_sym:
        return None
    try:
        url = f"https://stooq.com/q/d/l/?s={stooq_sym}&i=d&d1=19900101"
        r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        text = (r.text or "").strip()
        low = text.lower()
        if not text or low.startswith("<") or "no data" in low or "exceeded" in low or not low.startswith("date"):
            return None
        dates, highs, closes = [], [], []
        for ln in text.splitlines()[1:]:
            p = ln.split(",")
            if len(p) < 5:
                continue
            try:
                h, c = float(p[2]), float(p[4])
            except ValueError:
                continue
            dates.append(p[0]); highs.append(h); closes.append(c)
        return (dates, highs, closes) if len(closes) >= 2 else None
    except Exception as e:
        print(f"  Stooq 抓 {stooq_sym} 失敗：{e}")
        return None


def _merge_series(a, b):
    """依日期聯集合併兩來源序列：同一天最高價取兩者較大值(不低估歷史高點)、
    收盤取『兩者都有時以後者為準』；缺席的一方對合併結果沒有影響。
    用意：Yahoo 對部分指數常「有資料但少最新一天」，單純判斷 None／有值 抓不到這種情況，
    改成兩來源都抓、依日期聯集，任何一邊多出的最新一天都不會被漏掉。"""
    by_date = {}
    for s in (a, b):
        if not s:
            continue
        dates, highs, closes = s
        for d, h, c in zip(dates, highs, closes):
            if d in by_date:
                by_date[d] = (max(by_date[d][0], h), c)
            else:
                by_date[d] = (h, c)
    if not by_date:
        return None
    ds = sorted(by_date)
    return ds, [by_date[d][0] for d in ds], [by_date[d][1] for d in ds]


def finmind_get(dataset, max_retry=2, **params):
    """輕量版 FinMind 請求（僅供本檔的加權指數新鮮度補值用；失敗直接放棄不重試太久，
    不影響本檔其餘資料一律以 yfinance/Stooq 為主）。無 token 或套件缺失回空表。"""
    if requests is None or not FINMIND_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"}
    q = {"dataset": dataset, **params}
    wait = 10
    for _ in range(max_retry):
        try:
            resp = requests.get(FINMIND_URL, headers=headers, params=q, timeout=20)
        except Exception as e:
            print(f"  FinMind 連線錯誤：{e}"); time.sleep(wait); wait *= 2; continue
        if resp.status_code in (402, 429):
            print(f"  FinMind 流量上限，等待 {wait}s"); time.sleep(wait); wait *= 2; continue
        if resp.status_code != 200:
            return None
        return resp.json().get("data", [])
    return None


def fetch_finmind_twii_series():
    """加權指數新鮮度補值：TaiwanStockPrice 不含大盤本身，FinMind 需靠
    TaiwanVariousIndicators5Seconds（5秒頻加權指數快照，一次只能查一天）才能拿到 TAIEX。
    只補最近 TWII_FRESH_LOOKBACK_DAYS 個『日曆日』（非全歷史，控制配額），
    每天：當日最高快照當『高』、最後一筆快照當『收盤』。FinMind 是本站其餘資料的主來源、
    每天穩定到位，用它來補 yfinance 對 ^TWII 常見的「有資料但少最新一天」最可靠。"""
    if not FINMIND_TOKEN:
        return None
    dates, highs, closes = [], [], []
    today = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    for i in range(TWII_FRESH_LOOKBACK_DAYS):
        d = (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        rows = finmind_get("TaiwanVariousIndicators5Seconds", start_date=d)
        if i > 0:
            time.sleep(0.3)
        if not rows:
            continue
        vals = [r.get("TAIEX") for r in rows if r.get("TAIEX") not in (None, "")]
        vals = [float(v) for v in vals if v]
        if not vals:
            continue
        dates.append(d); highs.append(max(vals)); closes.append(vals[-1])
    if len(closes) < 1:
        return None
    order = sorted(range(len(dates)), key=lambda i: dates[i])
    return ([dates[i] for i in order], [highs[i] for i in order], [closes[i] for i in order])


def fetch_market_series(yahoo_sym):
    """yfinance ＋ Stooq ＋（僅 ^TWII 額外用 FinMind 補新鮮度）多來源合併，
    回傳 ((dates,highs,closes), 來源標記)。"""
    yf_s = fetch_yf_series(yahoo_sym)
    stooq_s = fetch_stooq_series(STOOQ_SYM.get(yahoo_sym))
    finmind_s = fetch_finmind_twii_series() if yahoo_sym == "^TWII" else None
    merged = None
    srcs = []
    for s, tag in ((yf_s, "yfinance"), (stooq_s, "stooq"), (finmind_s, "finmind")):
        if not s:
            continue
        srcs.append(tag)
        merged = s if merged is None else _merge_series(merged, s)
    if merged is None:
        return None, None
    return merged, "+".join(srcs)


def tsmc_from_db():
    """台積電保險：Yahoo 抓不到時改用本地 DB 的 2330（註：僅資料庫涵蓋區間，非全歷史）。"""
    if not os.path.exists(DB_PATH):
        return None
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute("SELECT date,high,close FROM price WHERE stock_id='2330' ORDER BY date").fetchall()
        con.close()
        dates = [r[0] for r in rows]
        highs = [r[1] for r in rows]
        closes = [r[2] for r in rows]
        r = _drawdown(dates, highs, closes)
        if r:
            r["db_only"] = True
        return r
    except Exception:
        return None


def get_market():
    out = {}
    for code, sym, name, kind in MARKET_TARGETS:
        series, src = fetch_market_series(sym)
        r = _drawdown(*series) if series else None
        if r is None and code == "TSMC":
            r = tsmc_from_db()
            src = "db" if r else src
        if r is not None:
            r["name"] = name
            r["kind"] = kind
            if code == "TWII" and series:   # 首頁「台股波動率」：用加權指數日收盤算年化歷史波動率
                vol = _realized_vol(series[2])
                if vol:
                    r["vol"] = vol
                    print(f"  台股波動率（加權指數年化）：20日 {vol['hv20']}% / 60日 {vol['hv60']}% / "
                          f"近一年位階 {vol['pct1y']}%")
            print(f"  市場資料 {name}：最高 {r['ath']}（{r['ath_date']}）/ 最近 {r['last']}（{r['last_date']}）/ "
                  f"回撤 {r['dd']}%［{src}］")
        else:
            print(f"  市場資料 {name}：取得失敗（yfinance/Stooq 皆無法取得）")
        out[code] = r
    return out


def build_html(results, history, market, trust, extras, flows, date, count, db_ok, gentime, industry=None, concepts=None):
    return (TEMPLATE
            .replace("/*__RESULTS__*/null", json.dumps(results, ensure_ascii=False))
            .replace("/*__HISTORY__*/null", json.dumps(history, ensure_ascii=False))
            .replace("/*__MARKET__*/null", json.dumps(market, ensure_ascii=False))
            .replace("/*__TRUST__*/null", json.dumps(trust, ensure_ascii=False))
            .replace("/*__EXTRAS__*/null", json.dumps(extras, ensure_ascii=False))
            .replace("/*__FLOWS__*/null", json.dumps(flows, ensure_ascii=False))
            .replace("/*__INDUSTRY__*/null", json.dumps(industry or {}, ensure_ascii=False))
            .replace("/*__CONCEPTS__*/null", json.dumps(concepts or {}, ensure_ascii=False))
            .replace("/*__DBOK__*/false", "true" if db_ok else "false")
            .replace("__DATE__", date or "")
            .replace("__GENTIME__", gentime)
            .replace("__COUNT__", str(count)))


def write_page(results, history, market, trust, extras, flows, date, db_ok, gentime, industry=None, concepts=None):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_html(results, history, market, trust, extras, flows, date, len(results), db_ok, gentime, industry, concepts))
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "台股看板", "short_name": "台股看板", "display": "standalone",
                   "orientation": "portrait", "background_color": "#000000",
                   "theme_color": "#000000", "start_url": "."}, f, ensure_ascii=False)


def main():
    gentime = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    print("抓首頁市場資料（加權指數 / 費半 / KOSPI / 台積電）…")
    market = get_market()
    trust = load_trust()
    extras = load_extras()
    flows = load_flows()
    have_db = os.path.exists(DB_PATH)
    industry = load_industry(DB_PATH) if have_db else {}
    concepts = tw_concepts.concept_map() if tw_concepts is not None else {}

    path = sys.argv[1] if len(sys.argv) > 1 else find_latest_csv()
    if not path or not os.path.exists(path):
        print("找不到選股 CSV，第二分頁顯示空清單（首頁與投信頁仍正常）。")
        nstk = write_stock_data(DB_PATH, OUT_DIR, industry) if have_db else 0
        write_page([], {}, market, trust, extras, flows, "", have_db, gentime, industry, concepts)
        print(f"已產生 {OUT_DIR}/index.html（無爆量清單・逐檔資料 {nstk} 檔・更新 {gentime}）")
        return
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    results = df.to_dict(orient="records")
    date = results[0].get("資料日", "") if results else ""
    # ⑨ 伺服器端計算「爆量月價值區間」，灌進每列（圖表不再內嵌歷史）
    if have_db:
        con = sqlite3.connect(DB_PATH)
        for r in results:
            try:
                z = month_value_zone(con, r.get("代號", ""))
            except sqlite3.Error:
                z = None
            if z:
                r["_zoneLabel"] = z["label"]; r["_zoneCls"] = z["cls"]; r["爆量月位階"] = z["pos"]
        con.close()
    # ④⑥ 產生逐檔資料檔（2005 起日線 + 近一年投信）＋首頁搜尋索引
    nstk = write_stock_data(DB_PATH, OUT_DIR, industry) if have_db else 0
    write_page(results, {}, market, trust, extras, flows, date, have_db, gentime, industry, concepts)
    tcount = len(trust.get("data", {})) if isinstance(trust, dict) else 0
    print(f"已產生 {OUT_DIR}/index.html（爆量 {len(results)}・投信候選 {tcount}・逐檔資料 {nstk} 檔・更新 {gentime}）")


# ============================================================
#  HTML（雙分頁 + 手機觸控 + PWA）。台股慣例：紅漲綠跌。
# ============================================================
TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="台股看板">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#000000">
<link rel="manifest" href="manifest.json">
<title>台股看板 ・ __DATE__</title>
<style>
  :root{
    --bg:#000000; --card:#121214; --card2:#1b1b1f; --border:#2a2a2f;
    --text:#f0f1f3; --muted:#9a9aa2; --dim:#67676e;
    --amber:#ffcf3a; --amber-s:rgba(255,207,58,.15);
    --up:#fb3b41; --down:#1ec77a;
    --blue:#5aa9ff; --blue-s:rgba(90,169,255,.12);
    --purple:#b794ff; --purple-s:rgba(183,148,255,.12);
    --ma5:#f5c518; --ma10:#e23fd0; --ma20:#27c4dc; --ma60:#c79a52; --ma240:#3b6fe0;
  }
  *{box-sizing:border-box;}
  body{margin:0; background:var(--bg); color:var(--text);
    font-family:system-ui,-apple-system,"PingFang TC","Noto Sans TC","Microsoft JhengHei","Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased; padding:16px 12px 36px; padding-top:calc(16px + env(safe-area-inset-top));}
  .num{font-variant-numeric:tabular-nums;}
  .wrap{max-width:1180px; margin:0 auto;}
  header h1{font-size:19px; font-weight:800; margin:0;}
  header h1 .bolt{color:var(--amber);}
  .sub{font-size:12px; color:var(--muted); margin-top:4px;}
  .hidden{display:none !important;}

  .tabbar{display:flex; gap:8px; margin:12px 0; background:transparent; padding:2px 0; border-bottom:1px solid var(--border);}
  .tab{flex:1; background:transparent; color:var(--muted); border:none; border-radius:99px; padding:9px 8px; font-size:14px; font-weight:700; cursor:pointer;}
  .tab.on{background:var(--amber); color:#000; font-weight:800;}
  .czentry{display:flex; align-items:center; justify-content:space-between; gap:10px; margin:0 0 14px; background:var(--card); border:1px solid var(--border); border-left:3px solid var(--amber); border-radius:8px; padding:12px 15px; color:var(--text); font-weight:700; font-size:14px; text-decoration:none;}
  .czentry .arr{color:var(--amber); font-weight:800; font-size:17px;}
  .czentry .czt2{display:block; font-size:11px; color:var(--muted); font-weight:600; margin-top:3px;}
  .czentry{display:flex; align-items:center; justify-content:space-between; gap:10px; margin:0 0 14px; background:var(--card); border:1px solid var(--border); border-left:3px solid var(--amber); border-radius:8px; padding:12px 15px; color:var(--text); font-weight:700; font-size:14px; text-decoration:none;}
  .czentry .czt2{font-size:11px; color:var(--muted); font-weight:600; margin-left:2px;}
  .czentry .arr{color:var(--amber); font-weight:800; font-size:16px;}
  /* 置頂精選：輝哥選股 */
  .czentry.pin{border:1px solid rgba(245,165,36,.5); border-left:4px solid var(--amber);
    background:linear-gradient(180deg, rgba(245,165,36,.12), rgba(245,165,36,.03)); font-size:15px;}
  .czentry.pin .pinbadge{display:inline-block; font-size:10px; font-weight:800; color:#000; background:var(--amber);
    border-radius:5px; padding:1px 6px; margin-right:7px; vertical-align:middle; letter-spacing:.5px;}

  /* 首頁回撤卡 */
  .ddcards{display:grid; grid-template-columns:1fr; gap:12px;}
  .ddcard{background:var(--card); border:1px solid var(--border); border-radius:9px; padding:16px 17px;}
  .flowwrap{margin-top:16px;}
  .flowtitle{font-size:13px; font-weight:700; color:var(--muted); margin:0 2px 10px;}
  .flowgrid{display:grid; grid-template-columns:1fr; gap:12px;}
  .fcard{background:var(--card); border:1px solid var(--border); border-radius:9px; padding:14px 16px;}
  .fcard .ft{font-size:12px; color:var(--muted); font-weight:600; margin-bottom:7px; display:flex; align-items:center; gap:8px;}
  .fcard .fd{font-size:11px; color:var(--dim); font-weight:500;}
  .fcard .fv{font-size:23px; font-weight:800; font-variant-numeric:tabular-nums; letter-spacing:-.5px;}
  .fcard .fu{font-size:12px; font-weight:600; color:var(--dim);}
  .fcard .fsub{font-size:12.5px; color:var(--muted); margin-top:6px; font-variant-numeric:tabular-nums;}
  .ddname{font-size:15px; font-weight:800; margin-bottom:10px;}
  .ddbig{font-size:13px; color:var(--muted); margin-bottom:10px;}
  .ddbig b{font-size:30px; font-weight:800; color:var(--amber); margin-left:4px; letter-spacing:.3px;}
  .ddbig b.flat{color:var(--up);}
  .ddbar{height:8px; background:#0e1626; border-radius:5px; overflow:hidden; margin-bottom:13px;}
  .ddbarfill{height:100%; background:var(--amber); border-radius:5px;}
  .ddrow{display:flex; align-items:baseline; gap:8px; padding:4px 0; border-top:1px solid var(--border);}
  .ddrow .k{font-size:12px; color:var(--dim); width:96px; flex:none;}
  .ddrow .v{font-size:17px; font-weight:800; font-variant-numeric:tabular-nums;}
  .ddrow .d{font-size:11px; color:var(--muted); margin-left:auto;}
  .ddna{color:var(--dim); font-size:13px; padding:8px 0;}
  .ddnote{font-size:12px; color:var(--dim); line-height:1.6; margin-top:14px; padding:13px 15px; background:var(--card); border:1px solid var(--border); border-radius:11px;}
  /* 首頁：台股波動率卡 */
  .volwrap{margin-top:12px;}
  .volcard{background:var(--card); border:1px solid var(--border); border-radius:11px; padding:14px 16px;}
  .volcard .vt{font-size:13px; color:var(--muted); font-weight:700; margin-bottom:9px; display:flex; align-items:center; gap:8px;}
  .volcard .vt .vd{font-size:11px; color:var(--dim); font-weight:500;}
  .volcard .vmain{display:flex; align-items:center; gap:11px;}
  .volcard .vbig{font-size:30px; font-weight:800; font-variant-numeric:tabular-nums; letter-spacing:-.5px; line-height:1;}
  .volcard .vlvl{font-size:12px; font-weight:800; padding:3px 10px; border-radius:99px; border:1px solid currentColor;}
  .volbar{height:8px; background:#0e1626; border-radius:5px; overflow:hidden; margin:12px 0 3px;}
  .volbarfill{height:100%; border-radius:5px;}
  .volcard .vsub{font-size:12.5px; color:var(--muted); margin-top:8px; font-variant-numeric:tabular-nums;}
  .volcard .vsub b{color:var(--text);}
  .searchwrap{position:relative; margin-bottom:14px;}
  .searchwrap input{width:100%; box-sizing:border-box; background:var(--card); border:1px solid var(--border); border-radius:11px; padding:12px 14px; color:var(--text); font-size:15px; outline:none;}
  .searchwrap input:focus{border-color:rgba(245,165,36,.5);}
  .sugbox{position:absolute; left:0; right:0; top:calc(100% + 6px); z-index:30; background:var(--card); border:1px solid var(--border); border-radius:11px; overflow:hidden; display:none; box-shadow:0 8px 28px rgba(0,0,0,.45);}
  .sugbox.on{display:block;}
  .sugitem{display:flex; align-items:center; gap:10px; padding:11px 14px; cursor:pointer; border-bottom:1px solid rgba(255,255,255,.04);}
  .sugitem:last-child{border-bottom:none;}
  .sugitem:active,.sugitem:hover{background:rgba(245,165,36,.1);}
  .sugitem .sc{font-weight:700; color:var(--amber); font-variant-numeric:tabular-nums; min-width:52px;}
  .sugitem .sn{flex:1; color:var(--text);}
  .sugitem .sm{font-size:12px; color:var(--dim);}
  .sugitem.dim{color:var(--dim); cursor:default; justify-content:center;}

  .cards{display:grid; grid-template-columns:repeat(2,1fr); gap:9px; margin-bottom:12px;}
  .stat{background:var(--card); border:1px solid var(--border); border-radius:11px; padding:13px 15px;}
  .stat .l{font-size:11px; color:var(--dim); margin-bottom:5px;}
  .stat .v{font-size:24px; font-weight:800; line-height:1;}
  .stat .s{font-size:11px; color:var(--muted); margin-top:4px;}

  .explain{background:var(--card); border:1px solid var(--border); border-radius:11px; margin-bottom:13px; overflow:hidden;}
  .explain summary{cursor:pointer; padding:13px 15px; font-size:13px; font-weight:700; color:var(--blue); list-style:none;}
  .explain summary::-webkit-details-marker{display:none;}
  .explain summary::before{content:"ⓘ "; }
  .explain[open] summary{border-bottom:1px solid var(--border);}
  .exbody{padding:6px 15px 14px; font-size:12.5px; line-height:1.65; color:var(--muted);}
  .exbody b{color:var(--text);}
  .exbody div{padding:5px 0; border-bottom:1px dashed var(--border);}
  .exbody div:last-child{border-bottom:none;}

  .panel{background:var(--card); border:1px solid var(--border); border-radius:12px; margin-bottom:14px;}
  .panel .ph{font-size:12px; font-weight:700; color:var(--muted); padding:13px 14px 2px;}
  .bars{padding:6px 14px 12px;}
  .barrow{display:grid; grid-template-columns:108px 1fr 48px; align-items:center; gap:8px; padding:3px 0;}
  .barrow .lbl{font-size:11px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .bartrack{height:15px; background:#0e1626; border-radius:4px; overflow:hidden;}
  .barfill{height:100%; border-radius:4px;}
  .barval{font-size:11px; font-weight:700; text-align:right;}
  .controls{display:flex; gap:7px; flex-wrap:wrap; align-items:center; margin-bottom:11px;}
  .controls input{background:var(--card); border:1px solid var(--border); border-radius:8px; padding:8px 12px; color:var(--text); font-size:14px; flex:1 1 140px; min-width:120px; outline:none;}
  .chip{background:var(--card); color:var(--muted); border:1px solid var(--border); border-radius:8px; padding:7px 13px; font-size:13px; cursor:pointer; font-weight:500;}
  .chip.on{background:var(--amber-s); color:var(--amber); border-color:rgba(245,165,36,.4);}
  .thr{display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin-bottom:11px;}
  .thrlbl{font-size:12px; color:var(--dim); margin-right:2px;}
  .thrbtn{background:var(--card); color:var(--muted); border:1px solid var(--border); border-radius:8px; padding:7px 13px; font-size:13px; cursor:pointer; font-weight:700;}
  .thrbtn.on{background:var(--amber-s); color:var(--amber); border-color:rgba(245,165,36,.4);}
  /* 概念分群：群組標題列 + 分群切換鈕 */
  .gtog{background:var(--card); color:var(--muted); border:1px solid var(--border); border-radius:8px; padding:6px 11px; font-size:12px; cursor:pointer; font-weight:700; white-space:nowrap;}
  .gtog.on{background:var(--amber-s); color:var(--amber); border-color:rgba(245,165,36,.4);}
  tr.grouphdr td{text-align:left; background:var(--card2); border-top:2px solid var(--border); padding:6px 10px; font-weight:800; font-size:13px; color:var(--text);}
  tr.grouphdr .ghlbl{position:sticky; left:10px; display:inline-block;}
  tr.grouphdr .gchip{display:inline-block; font-size:10px; font-weight:700; padding:1px 6px; border-radius:5px; margin-right:7px;}
  tr.grouphdr.gc .gchip{background:rgba(77,159,255,.16); color:#6fb0ff;}
  tr.grouphdr.gi .gchip{background:rgba(94,111,134,.18); color:#93a3b8;}
  tr.grouphdr .gcount{color:var(--dim); font-weight:600; font-size:11px; margin-left:6px;}
  .rotseg{display:inline-flex; gap:4px; margin:0 6px; vertical-align:middle;}
  .rotseg .gtog{padding:4px 10px;}
  .zone{display:inline-block; padding:2px 7px; border-radius:6px; font-size:11px; font-weight:700; border:1px solid; white-space:nowrap;}
  .z-value{background:var(--amber-s); color:var(--amber); border-color:rgba(245,165,36,.45);}
  .z-upper{background:var(--blue-s); color:var(--blue); border-color:rgba(77,159,255,.3);}
  .z-above{background:rgba(94,111,134,.14); color:var(--muted); border-color:rgba(94,111,134,.3);}
  .z-below{background:rgba(94,111,134,.1); color:var(--dim); border-color:rgba(94,111,134,.25);}
  .zpos{font-size:10px; color:var(--dim); margin-left:5px;}
  .fchips{display:flex; gap:7px; flex-wrap:wrap; margin-bottom:13px;}
  .fchip{background:var(--card); color:var(--muted); border:1px solid var(--border); border-radius:8px; padding:8px 13px; font-size:13px; font-weight:600; cursor:pointer;}
  .fchip.on{background:var(--amber-s); color:var(--amber); border-color:rgba(245,165,36,.4);}
  .toptop{display:grid; grid-template-columns:1fr; gap:14px;}
  @media(min-width:640px){ .toptop{grid-template-columns:1fr 1fr;} }
  .tpanel{background:var(--card); border:1px solid var(--border); border-radius:9px; overflow:hidden;}
  .tphd{font-size:13px; font-weight:800; padding:11px 14px; border-bottom:1px solid var(--border);}
  .tphd.buy{color:var(--up);} .tphd.sell{color:var(--down);}
  .frow{position:relative; display:flex; align-items:center; gap:9px; padding:9px 13px; border-bottom:1px solid rgba(255,255,255,.04); font-size:13px;}
  .frow:last-child{border-bottom:none;}
  .frow .fbar{position:absolute; left:0; top:0; bottom:0; z-index:0;}
  .frow.buy .fbar{background:rgba(255,77,79,.10);}
  .frow.sell .fbar{background:rgba(34,197,94,.10);}
  .frow>*{position:relative; z-index:1;}
  .frk{width:18px; text-align:center; font-weight:700; color:var(--dim); font-size:12px;}
  .fnm2{flex:1; min-width:0; font-weight:600; cursor:pointer; display:flex; flex-direction:column; justify-content:center;}
  .fnmtxt{white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .fnm2 i{color:var(--dim); font-style:normal; font-size:11px; margin-left:4px;}
  /* 產業類型標籤：股名下方小字 */
  .indtag{display:block; font-size:10.5px; font-weight:600; color:#7c8aa0; letter-spacing:.2px; margin-top:1px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .indtag.inline{display:inline-block; margin:0 0 0 6px; padding:1px 6px; border:1px solid var(--border); border-radius:6px; color:#8aa0b6; background:rgba(255,255,255,.03);}
  /* 概念股標籤（個股K線頁）：可換行小膠囊 */
  .cvconcepts{display:flex; flex-wrap:wrap; gap:5px; padding:6px 14px 0;}
  .cvconcepts:empty{display:none;}
  .cchip{display:inline-block; font-size:11px; font-weight:600; line-height:1.5; padding:1px 8px; border-radius:999px;
    color:#a7c5e8; border:1px solid rgba(96,165,250,.35); background:rgba(96,165,250,.10); white-space:nowrap;}
  .cchip.concept-sm{font-size:10px; padding:0 6px; margin-left:5px; vertical-align:middle;}
  .fval{font-weight:800; font-variant-numeric:tabular-nums; white-space:nowrap;}
  .fval small{font-weight:600; color:var(--dim); font-size:10px; margin-left:1px;}
  .fcg{width:60px; text-align:right; font-size:12px; font-variant-numeric:tabular-nums;}
  .tpempty{padding:26px; text-align:center; color:var(--dim); font-size:13px;}
  .hint{font-size:11px; color:var(--dim); width:100%;}
  /* === 資金流向：熱圖 + 產業資金輪動 === */
  .flowsec{margin-top:20px; padding-top:16px; border-top:1px solid var(--border);}
  .fsec-h{font-size:15px; font-weight:800; color:var(--text); margin:0 2px 9px; display:flex; align-items:baseline; gap:8px; flex-wrap:wrap;}
  .fsec-sub{font-size:11px; font-weight:600; color:var(--dim); letter-spacing:.2px;}
  .heatlegend{display:flex; align-items:center; gap:5px; margin:0 2px 9px; flex-wrap:wrap;}
  .heatlegend .hl{display:flex; align-items:center; gap:4px; font-size:11px; color:var(--muted);}
  .heatlegend .hl i{width:16px; height:11px; border-radius:2px; display:inline-block;}
  .heatlegend .hl.dim{color:var(--dim); margin-left:auto;}
  .heatbox{position:relative; width:100%; height:62vh; min-height:380px; max-height:680px; background:var(--card); border:1px solid var(--border); border-radius:10px; overflow:hidden;}
  .heatsec{position:absolute; overflow:hidden;}
  .heatsec>.hsl{position:absolute; top:0; left:0; right:0; padding:2px 5px; font-size:10px; font-weight:700; color:rgba(255,255,255,.78); background:rgba(0,0,0,.34); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; z-index:2; pointer-events:none;}
  .htile{position:absolute; overflow:hidden; border:1px solid rgba(0,0,0,.45); cursor:pointer; display:flex; flex-direction:column; justify-content:center; align-items:center; text-align:center; line-height:1.05;}
  .htile .hn{font-weight:800; color:#fff; text-shadow:0 1px 2px rgba(0,0,0,.6); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:100%;}
  .htile .hc{font-weight:700; color:rgba(255,255,255,.92); text-shadow:0 1px 2px rgba(0,0,0,.6);}
  .htile.tiny .hn,.htile.tiny .hc{display:none;}
  /* 產業資金輪動長條 */
  .rotbox{display:flex; flex-direction:column; gap:0; background:var(--card); border:1px solid var(--border); border-radius:10px; overflow:hidden;}
  .rotrow{border-bottom:1px solid rgba(255,255,255,.05);}
  .rotrow:last-child{border-bottom:none;}
  .rothead{display:flex; align-items:center; gap:10px; padding:10px 12px; cursor:pointer;}
  .rothead:hover{background:rgba(255,255,255,.02);}
  .rotleft{flex:0 0 38%; min-width:0;}
  .rotnm{font-size:13px; font-weight:700; color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .rotnm .rcar{display:inline-block; width:12px; color:var(--dim); font-size:10px; transition:transform .15s;}
  .rotrow.open .rcar{transform:rotate(90deg); color:var(--amber);}
  .rotmeta{font-size:10.5px; color:var(--dim); margin-top:1px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .rotbarwrap{flex:1; position:relative; height:26px; min-width:60px;}
  .rotaxis{position:absolute; top:0; bottom:0; left:50%; width:1px; background:rgba(255,255,255,.14);}
  .rotbar{position:absolute; top:5px; height:16px; border-radius:3px;}
  .rotbar.pos{background:linear-gradient(90deg,rgba(251,59,65,.55),var(--up)); left:50%;}
  .rotbar.neg{background:linear-gradient(270deg,rgba(30,199,122,.55),var(--down)); right:50%;}
  .rotval{position:absolute; top:5px; font-size:11px; font-weight:800; font-variant-numeric:tabular-nums; line-height:16px; color:var(--text);}
  .rotmom{flex:0 0 64px; text-align:right; font-size:11px; font-variant-numeric:tabular-nums;}
  .rotmom .ml{font-size:9px; color:var(--dim); display:block; line-height:1;}
  .rotpanel{display:none; padding:0 0 4px; background:rgba(0,0,0,.16);}
  .rotrow.open .rotpanel{display:block;}
  /* 族群成分股緊湊表（仿處置神器） */
  .gtbl-wrap{overflow-x:auto; -webkit-overflow-scrolling:touch;}
  table.gtbl{width:100%; min-width:560px; border-collapse:collapse; font-size:12px;}
  table.gtbl th{position:sticky; top:0; background:var(--card2); color:var(--dim); font-size:10px; font-weight:600; padding:6px 8px; text-align:right; white-space:nowrap; border-bottom:1px solid var(--border);}
  table.gtbl th.frz,table.gtbl td.frz{position:sticky; left:0; z-index:2; text-align:left; background:var(--card);}
  table.gtbl th.frz{z-index:4; background:var(--card2);}
  table.gtbl td{padding:6px 8px; text-align:right; white-space:nowrap; border-bottom:1px solid rgba(255,255,255,.05); font-variant-numeric:tabular-nums;}
  table.gtbl tr:last-child td{border-bottom:none;}
  table.gtbl tr.side-up td.frz{box-shadow:inset 3px 0 0 var(--up);}
  table.gtbl tr.side-down td.frz{box-shadow:inset 3px 0 0 var(--down);}
  table.gtbl tbody tr{cursor:pointer;}
  table.gtbl .gnm{font-weight:700; color:var(--text);}
  table.gtbl .gsub{font-size:10px; color:var(--dim);}
  table.gtbl .cv.up{color:var(--up);} table.gtbl .cv.down{color:var(--down);} table.gtbl .cv.dim{color:var(--dim);}
  table.gtbl .cv2{font-size:10px; color:var(--dim); display:block; margin-top:1px;}
  /* 可垂直＋水平捲動的盒；向下滑動時表頭(sticky)固定 */
  .tablewrap{overflow:auto; max-height:74vh; -webkit-overflow-scrolling:touch; border:1px solid var(--border); border-radius:12px; background:var(--card); overscroll-behavior:contain;}
  /* 移到列表上方的水平捲動bar（與表格同步） */
  .hbar{overflow-x:auto; overflow-y:hidden; height:13px; margin-bottom:5px; border-radius:8px; background:var(--card2);}
  .hbar>div{height:1px;}
  table{width:100%; border-collapse:collapse; font-size:13px; min-width:860px;}
  thead th{position:sticky; top:0; z-index:3; background:var(--card2);}
  th{padding:10px 11px; text-align:right; color:var(--dim); font-weight:600; font-size:11px; white-space:nowrap; cursor:pointer; border-bottom:1px solid var(--border);}
  th.l, td.l{text-align:left;}
  th .ar{color:var(--amber); margin-left:2px;}
  td{padding:10px 11px; text-align:right; border-bottom:1px solid var(--border); white-space:nowrap;}
  tr:last-child td{border-bottom:none;}
  .code{font-weight:700; color:var(--amber);}
  .nm{cursor:pointer; border-bottom:1px dashed var(--dim);}
  .mkt{font-size:11px; padding:2px 7px; border-radius:4px; font-weight:600;}
  .mkt.twse{background:var(--blue-s); color:var(--blue);} .mkt.tpex{background:var(--purple-s); color:var(--purple);}
  .lim{font-size:10px; padding:1px 5px; border-radius:3px; color:#fff; font-weight:700; margin-left:5px;}
  .vr{font-weight:800;}
  .scorewrap{display:inline-flex; align-items:center; gap:8px; justify-content:flex-end;}
  .scoretrack{width:60px; height:6px; background:var(--border); border-radius:3px; overflow:hidden;}
  .scorefill{height:100%; border-radius:3px;}
  .scoreval{font-weight:700; min-width:30px; text-align:right;}
  .tags{text-align:left; white-space:nowrap;}
  .tag{display:inline-block; padding:2px 7px; border-radius:4px; font-size:11px; font-weight:500; margin:1px 3px 1px 0; border:1px solid;}
  .foot{text-align:center; color:var(--dim); font-size:11px; margin-top:16px;}
  .srclink{color:var(--amber); text-decoration:none;} .srclink:hover{text-decoration:underline;}

  #cv{position:fixed; inset:0; background:#070b14; z-index:50; display:none; flex-direction:column; padding-top:env(safe-area-inset-top);}
  #cv.open{display:flex;}
  .cvhead{display:flex; align-items:center; gap:10px; padding:10px 14px; border-bottom:1px solid var(--border); flex-wrap:wrap;}
  .back{background:var(--card); border:1px solid var(--border); color:var(--muted); border-radius:8px; padding:7px 12px; font-size:14px; cursor:pointer;}
  .cvtitle{font-size:17px; font-weight:800;}
  .cvtitle .c{color:var(--amber); margin-right:7px;}
  .cvchg{font-size:14px; font-weight:700;}
  /* 表頭空白處：發行張數 + 扣董監大戶後流通張數 */
  .cvfloat{display:flex; align-items:baseline; gap:5px; font-size:12px; color:var(--muted); flex-wrap:wrap;}
  .cvfloat .cvfk{color:var(--dim);}
  .cvfloat .cvfv{color:var(--text); font-weight:700; font-variant-numeric:tabular-nums;}
  .cvfloat .cvfsep{color:var(--border);}
  .cvfloat .cvfnote{color:var(--dim); font-size:11px;}
  .pswitch{display:flex; gap:4px; margin-left:auto;}
  .pbtn{background:var(--card); border:1px solid var(--border); color:var(--muted); border-radius:7px; padding:7px 15px; font-size:14px; cursor:pointer; font-weight:600;}
  .pbtn.on{background:var(--amber-s); color:var(--amber); border-color:rgba(245,165,36,.4);}
  .rotoggle{display:block; width:100%; text-align:left; background:var(--card); color:var(--amber); border:none; border-bottom:1px solid var(--border); padding:7px 14px; font-size:12px; font-weight:700; cursor:pointer;}
  .readout{display:flex; gap:13px; flex-wrap:wrap; padding:8px 14px; font-size:13px; border-bottom:1px solid var(--border); background:var(--card);}
  .readout .it{display:flex; gap:5px;}
  .readout:not(.full) .it.ext{display:none;}   /* 收摺時隱藏 MA/布林/MACD 等進階數據 */
  .readout .k{color:var(--dim);}
  .readout .v{font-weight:700; font-variant-numeric:tabular-nums;}
  .malegend{display:flex; gap:11px; flex-wrap:wrap; padding:6px 14px 0; font-size:11px;}
  .malegend span{display:flex; align-items:center; gap:4px; color:var(--muted);}
  .malegend i{width:13px; height:3px; border-radius:2px; display:inline-block;}
  .chartbox{flex:1; position:relative; min-height:0;}
  #chartCanvas{position:absolute; inset:0; width:100%; height:100%; touch-action:none;}
  @media(min-width:640px){ .cards{grid-template-columns:repeat(4,1fr);} .ddcards{grid-template-columns:repeat(3,1fr);} .flowgrid{grid-template-columns:repeat(3,1fr);} body{padding:22px 18px 40px;} }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1><span class="bolt">⚡</span> 台股看板</h1>
    <div class="sub">更新 __GENTIME__（台北）</div>
  </header>

  <div class="tabbar">
    <button class="tab on" data-tab="home">首頁</button>
    <button class="tab" data-tab="screen">爆量起漲</button>
    <button class="tab" data-tab="trust">投信連買</button>
    <button class="tab" data-tab="flow">資金流向</button>
  </div>

  <a class="czentry pin" href="hui.html">
    <span><span class="pinbadge">置頂</span>🐉 輝哥選股<span class="czt2">均線突破四海遊龍 ＋ 盤整突破・依概念族群分類・附公式說明</span></span>
    <span class="arr">→</span>
  </a>
  <a class="czentry" href="market.html" style="border-left-color:var(--blue)">
    <span>📊 市場分析<span class="czt2">台美股每日盤勢・資金流向・族群雷達・每日監控清單</span></span>
    <span class="arr" style="color:var(--blue)">→</span>
  </a>
  <a class="czentry" href="chuzhi.html" style="margin-top:-6px">
    <span>🚦 處置股專區<span class="czt2">即將／確定／處置中／出關 監控 ＋ 實戰SOP</span></span>
    <span class="arr">→</span>
  </a>

  <!-- 分頁一：首頁 -->
  <div class="tabpane" id="tab-home">
    <div class="searchwrap">
      <input id="stkq" placeholder="🔍 搜尋任意股票（代號或名稱）看完整 K 線…" autocomplete="off">
      <div class="sugbox" id="sugbox"></div>
    </div>
    <div class="ddcards" id="ddcards"></div>
    <div class="volwrap" id="volwrap"></div>
    <div class="flowwrap" id="flowwrap"></div>
    <div class="ddnote">
      <b style="color:var(--muted)">回撤</b>＝最近一次收盤距「歷史最高價（盤中最高點）」的跌幅。<br>
      數字越大代表離前高越遠。台股加權指數與費城半導體為指數點數，台積電為股價。<br>
      美股費半依美國收盤，台北下午更新時通常為「前一個美股交易日」。<br>
      <b style="color:var(--muted)">台股波動率</b>＝加權指數近 20 個交易日「日報酬」的年化標準差（%），數字越大＝盤勢越震盪；
      「近一年位階」為目前波動率在近一年區間的百分位（越高＝相對自己近一年越劇烈）。
    </div>
  </div>

  <!-- 分頁二：選股 -->
  <div class="tabpane hidden" id="tab-screen">
    <div class="sub" id="subtitle" style="margin:0 0 12px">資料日 __DATE__ ・ 共 __COUNT__ 檔</div>
    <div class="cards" id="cards"></div>
    <details class="explain">
      <summary>指標說明（點開）</summary>
      <div class="exbody">
        <div><b>月均量</b>：最近 20 個交易日的平均成交量（張）。代表這檔平常的量能水準。</div>
        <div><b>量比</b>：今日量 ÷ 月均量。例如 <b>3x</b> 表示今天的量是平常的 3 倍 →「爆量」。本表門檻為 ≥ 2x。</div>
        <div><b>5日量/月量</b>：最近 5 日均量 ÷ 月均量。&gt;1 代表近期量能持續放大，不是只爆一天。</div>
        <div><b>季線乖離%</b>：收盤距 60 日均線（季線）的距離。數字太大代表短線漲多、追高風險高（本表上限約 30%）。</div>
        <div><b>評分</b>：綜合爆量強度、量能持續、突破季高、均線多頭排列等的 0–100 分，僅供「排序」參考，非買賣建議。</div>
        <div><b>爆量月位階</b>：先找歷史上「成交量最大的那個月份」，取該月 K 的最高、最低價。看現價落在這區間的位置：<b style="color:var(--amber)">近爆量低★</b>＝現價在該月中價~低價之間（最貼近大量低點、相對有買進價值）；<b style="color:var(--blue)">爆量月上半</b>＝中價~高價之間；月量高之上＝已突破該月高點；破爆量低＝已跌破該月低點。百分比＝位置(0%＝月低、100%＝月高)。可點此欄由小到大排序，把最接近大量低點的排在前面。</div>
        <div><b>強度標記</b>：符合的偏多條件標籤，如 突破季高、月線翻揚、站上季線、季線翻揚、多頭排列、站上年線。</div>
        <div style="color:var(--dim)">本表為機械式初篩，進場前仍需看籌碼（三大法人／主力）、消息面與基本面。</div>
      </div>
    </details>
    <div class="panel"><div class="ph">量比排行（前 20）</div><div class="bars" id="bars"></div></div>
    <div class="controls">
      <input id="q" placeholder="搜尋代號或名稱…" autocomplete="off">
      <button class="chip on" data-mkt="全部">全部</button>
      <button class="chip" data-mkt="上市">上市</button>
      <button class="chip" data-mkt="上櫃">上櫃</button>
      <button class="gtog on" id="screenGtog" title="依概念股/產業族群分組排列">☰ 依概念分群</button>
      <span class="hint">點欄位排序 ・ 點名稱看K線 ・ 分群時同概念(退回產業)歸一組</span>
    </div>
    <div class="hbar" id="screenHbar"><div></div></div>
    <div class="tablewrap" id="screenWrap"><table><thead><tr id="thead"></tr></thead><tbody id="tbody"></tbody></table></div>
  </div>

  <!-- 分頁三：投信連買 -->
  <div class="tabpane hidden" id="tab-trust">
    <div class="sub" style="margin:0 0 10px">投信連續買超 ・ 籌碼面 ・ 資料日 <span id="trustdate">—</span></div>
    <div class="thr">
      <span class="thrlbl">每日門檻</span>
      <button class="thrbtn on" data-thr="50">50張</button>
      <button class="thrbtn" data-thr="100">100張</button>
      <button class="thrbtn" data-thr="200">200張</button>
      <button class="thrbtn" data-thr="500">500張</button>
      <button class="thrbtn" data-thr="1000">1000張</button>
      <button class="gtog on" id="trustGtog" title="依概念股/產業族群分組排列" style="margin-left:8px">☰ 依概念分群</button>
    </div>
    <details class="explain">
      <summary>篩選邏輯與指標說明（點開）</summary>
      <div class="exbody">
        <div><b>怎麼篩</b>：最近一個月內，投信「<b>連續 ≥3 個交易日</b>」每日淨買都 <b>≥ 你選的張數</b>；且<b>現價 ≤ 連買期間最高價</b>，或<b>現價 &lt; 投信成本均價</b>（＝投信買了、但股價還沒漲上去 / 甚至跌破投信成本）。</div>
        <div><b>投信買超佔比</b>：連買期間 投信淨買張數 ÷ 同期總成交張數。<b>越高＝投信主導、籌碼集中</b>（本頁最關鍵指標）。</div>
        <div><b>連買天數 / 累計張數</b>：投信吃貨的「久」與「重」。</div>
        <div><b>投信成本均價</b>：連買期間以每日投信淨買量加權的收盤均價（近似投信平均成本）。</div>
        <div><b>距成本%</b>：現價 ÷ 投信成本 −1。負值（綠）＝現價已跌破投信成本，投信暫時套牢（雙面刃：可能加碼護盤，也可能停損）。</div>
        <div><b>距高點%</b>：連買最高價 ÷ 現價 −1。越大＝離投信買的高點越遠、潛在補漲空間越大。</div>
        <div><b>連買漲幅%</b>：連買期間股價漲跌幅。越小＝越「還沒發動」。</div>
        <div><b>仍在買</b>：投信連買是否延續到最新一天（是＝籌碼仍有支撐）。</div>
        <div><b>賣回%</b>：連買結束後投信又賣回了多少（佔累計買超）。<b>≥60% 直接從清單剔除</b>（視為投信已落跑）；數字越低越好，0% 最佳。</div>
        <div><b>評分</b>：以「投信主導性(佔比)」為核心，加吃貨強度、補漲空間、貼近投信成本；已大漲或被部分賣回者扣分。<b>僅供排序，非投資建議</b>。</div>
        <div style="color:var(--dim)">註：投信買賣超為盤後資料，通常較股價晚約一個交易日；目前涵蓋上市，上櫃稍後補上。</div>
      </div>
    </details>
    <div class="tablewrap"><table><thead><tr id="trusthead"></tr></thead><tbody id="trustbody"></tbody></table></div>
  </div>

  <!-- 分頁四：資金流向 -->
  <div class="tabpane hidden" id="tab-flow">
    <div class="sub" style="margin:0 0 10px">大戶 / 投信資金流向 ・ 買賣超金額 TOP10 ・ 資料日 <span id="flowdate">—</span></div>
    <div class="fchips" id="fchips">
      <button class="fchip on" data-fk="big_d">大戶·當日</button>
      <button class="fchip" data-fk="big_5">大戶·近5日</button>
      <button class="fchip" data-fk="big_20">大戶·近20日</button>
      <button class="fchip" data-fk="big_60">大戶·近60日</button>
      <button class="fchip" data-fk="trust_d">投信·當日</button>
      <button class="fchip" data-fk="trust_5">投信·近5日</button>
      <button class="fchip" data-fk="trust_20">投信·近20日</button>
      <button class="fchip" data-fk="trust_60">投信·近60日</button>
    </div>
    <div class="toptop" id="toptop"></div>
    <details class="exp"><summary>說明</summary>
      <div class="expbody">
        <div><b>大戶</b>＝<b>三大法人合計</b>（外資＋投信＋自營）買賣超，作為「大戶／主力資金」的免費替代指標（無單獨大戶分點免費來源）。<b>投信</b>＝投信單獨買賣超。</div>
        <div><b>金額(億)</b>＝買賣超張數 × 當日收盤估算；<b>當日</b>＝最近一個交易日，<b>近5/20/60日</b>＝最近 5／20／60 個交易日累計。紅＝買超、綠＝賣超。點名稱可看 K 線。</div>
        <div style="color:var(--dim)">資料為證交所 T86 盤後（目前涵蓋上市；上櫃稍後補上），通常較股價晚約一個交易日。</div>
      </div>
    </details>

    <!-- 市場當天交易熱圖 -->
    <div class="flowsec">
      <div class="fsec-h">🗺️ 市場當天交易熱圖<span class="fsec-sub">方塊大小＝當日成交值 ・ 顏色＝漲跌幅(紅漲綠跌) ・ 依產業分群 ・ 資料日 <span id="heatdate">—</span></span></div>
      <div class="heatlegend">
        <span class="hl"><i style="background:rgba(30,199,122,.8)"></i>跌</span>
        <span class="hl"><i style="background:rgba(30,199,122,.35)"></i></span>
        <span class="hl"><i style="background:rgba(120,120,130,.3)"></i>平</span>
        <span class="hl"><i style="background:rgba(251,59,65,.35)"></i></span>
        <span class="hl"><i style="background:rgba(251,59,65,.8)"></i>漲</span>
        <span class="hl dim">方塊大小＝成交值・點方塊看K線</span>
      </div>
      <div class="heatbox" id="heatbox"><div class="tpempty">熱圖資料載入中…</div></div>
    </div>

    <!-- 120 日產業資金輪動 -->
    <div class="flowsec">
      <div class="fsec-h">🔄 資金輪動<span class="rotseg"><button class="gtog on" data-rm="ind">產業</button><button class="gtog" data-rm="concept">概念股</button></span><span class="fsec-sub">近 <span id="rotdays">120</span> 交易日 三大法人累計買賣超(億) ・ 紅＝流入 綠＝流出 ・ 點族群展開成分股</span></div>
      <div class="rotbox" id="rotbox"><div class="tpempty">資金輪動資料載入中…</div></div>
      <details class="exp"><summary>說明 / 指標</summary>
        <div class="expbody">
          <div><b>族群</b>：把同一產業鏈／概念的個股整合成一群（如「電子上游-IC設計」「航運-貨櫃」）。主要個股用<b>產業鏈概念</b>分群，其餘退回 FinMind 大分類。</div>
          <div><b>近120日(億)</b>：該族群所有成分股近 120 個交易日<b>三大法人累計買賣超金額</b>加總。<b>正(紅)＝資金淨流入、負(綠)＝淨流出</b>。由大到小排序，一眼看出 120 天內資金<b>從哪些族群流出、轉進哪些族群</b>。</div>
          <div><b>近20日</b>：族群最近 20 日法人淨額，看資金<b>現在</b>還在流入(▲)或已轉為流出(▼)＝輪動方向。</div>
          <div style="margin-top:4px"><b>成分股指標</b>（點族群展開，比照處置中個股）：</div>
          <div><b>股價／漲幅</b>＝今日收盤與漲跌幅。<b>位階</b>＝20日布林通道級距(+10上軌偏高、0月線、−10下軌偏低)。<b>斜率</b>＝月線(20MA)一日斜率%(&gt;1%強勢)。</div>
          <div><b>主5／主10</b>＝近5/10日三大法人集中度%＝Σ法人買賣超張÷Σ成交量張×100，正(紅)＝法人買超集中、負(綠)＝派發（市場版以三大法人替代分點主力）。</div>
          <div><b>法20(億)</b>＝個股近20日三大法人淨額，看族群裡<b>法人實際在買哪幾檔</b>。<b>量比</b>＝今量÷20日均量(&gt;2爆量)。<b>季乖離%</b>＝距季線(60MA)距離，過大＝短線漲多、追高風險。</div>
          <div style="color:var(--dim)">三大法人買賣超為證交所 T86 盤後（目前涵蓋上市），通常較股價晚約一個交易日。僅供研究，非投資建議。</div>
        </div>
      </details>
    </div>
  </div>

  <div class="foot">資料來源：<a href="https://finmindtrade.com" target="_blank" rel="noopener" class="srclink">FinMind</a>、證交所／櫃買、Yahoo Finance ・ 僅供研究，非投資建議</div>
</div>

<div id="cv">
  <div class="cvhead">
    <button class="back" onclick="closeChart()">◀ 返回</button>
    <div class="cvtitle"><span class="c" id="cvCode"></span><span id="cvName"></span><span class="indtag inline" id="cvInd"></span></div>
    <div class="cvchg" id="cvChg"></div>
    <div class="cvfloat" id="cvFloat"></div>
    <div class="pswitch"><button class="pbtn on" data-p="D">日K</button><button class="pbtn" data-p="W">週K</button><button class="pbtn" data-p="M">月K</button><button class="pbtn" id="volModeBtn" onclick="toggleVol()" title="循環切換：量 → 投信 → 外資 → 400張大戶" style="margin-left:7px">副圖:量</button><button class="pbtn" id="macdModeBtn" onclick="toggleMacd()" title="循環切換：MACD → RSI → KD → 主力" style="margin-left:5px">下圖:MACD</button></div>
  </div>
  <div class="cvconcepts" id="cvConcepts"></div>
  <button class="rotoggle" id="roToggle" onclick="toggleReadout()">▾ 展開指標數據(MA/布林/MACD…)</button>
  <div class="readout" id="readout"></div>
  <div class="malegend">
    <span><i style="background:var(--ma5)"></i>MA5</span><span><i style="background:var(--ma10)"></i>MA10</span>
    <span><i style="background:var(--ma20)"></i>MA20</span><span><i style="background:var(--ma60)"></i>MA60</span>
    <span><i style="background:var(--ma240)"></i>MA240</span><span><i style="background:#8aa0b6"></i>布林20</span>
    <span style="color:var(--dim)">點K棒鎖定十字線（日期／價位＋副圖同步）・ 單指拖移掃描 ・ 雙指縮放 ・ 上方按鈕切換 副圖(量/投信/外資/400張大戶)、下圖(MACD/RSI/KD/主力)</span>
    <span style="color:var(--dim)">籌碼資料（三大法人／400張大戶／發行張數）資料來源：<a href="https://finmindtrade.com" target="_blank" rel="noopener" class="srclink">FinMind</a></span>
  </div>
  <div class="chartbox"><canvas id="chartCanvas"></canvas></div>
</div>

<script>
const RESULTS = /*__RESULTS__*/null;
const HISTORY = /*__HISTORY__*/null;
const MARKET  = /*__MARKET__*/null;
const TRUST   = /*__TRUST__*/null;
const EXTRAS  = /*__EXTRAS__*/null;
const FLOWS   = /*__FLOWS__*/null;
const INDUSTRY= /*__INDUSTRY__*/null;
const CONCEPTS= /*__CONCEPTS__*/null;   // {sid: ["ABF載板","被動元件",...]}（概念股標籤）
// 建置版本：每次重建都變（來自產生時間），給逐檔資料檔加 ?v= 版本參數，
// 避免瀏覽器/CDN 送出舊的快取 JSON（否則重新部署後主力/發行等新資料看不到，需手動強制重整）。
const BUILD_V=("__GENTIME__").replace(/[^0-9]/g,"")||"0";
const DB_OK   = /*__DBOK__*/false;
/* 產業類型標籤：股名下方小字。indLabel 回字串，indTag 回 HTML(含樣式)。 */
function indLabel(sid){ try{ return (INDUSTRY&&INDUSTRY[sid])?INDUSTRY[sid]:""; }catch(e){ return ""; } }
function indTag(sid){ const s=indLabel(sid); return s?`<span class="indtag">${s}</span>`:""; }
/* 概念股標籤：conceptList 回陣列，conceptChips 回K線頁膠囊列，conceptInline 回搜尋列小標。 */
function conceptList(sid){ try{ return (CONCEPTS&&CONCEPTS[sid])?CONCEPTS[sid]:[]; }catch(e){ return []; } }
function conceptChips(sid){ const a=conceptList(sid); return a.length?a.map(c=>`<span class="cchip">${c}</span>`).join(""):""; }
function conceptInline(sid){ const a=conceptList(sid); return a.length?`<span class="cchip concept-sm">${a[0]}${a.length>1?" +"+(a.length-1):""}</span>`:""; }
/* 依「概念(優先，取主概念=第一個)／產業別(退回)／未分類」把清單分群；概念群在前、產業次之、未分類最後，
   同類再依檔數多→少排序。每群內維持傳入清單的既有排序。多概念個股只歸入主概念，不重複出現。 */
function groupByConcept(rows, sidOf){
  const gm={};
  for(const r of rows){
    const sid=sidOf(r), cs=conceptList(sid);
    let name, isC;
    if(cs.length){ name=cs[0]; isC=true; }
    else { const ind=indLabel(sid); name=ind||"未分類"; isC=false; }
    (gm[name]||(gm[name]={name, isConcept:isC, rows:[]})).rows.push(r);
  }
  const arr=Object.keys(gm).map(k=>gm[k]);
  arr.sort((a,b)=>{
    const ap=a.name==="未分類"?2:(a.isConcept?0:1), bp=b.name==="未分類"?2:(b.isConcept?0:1);
    if(ap!==bp) return ap-bp;
    if(b.rows.length!==a.rows.length) return b.rows.length-a.rows.length;
    return a.name.localeCompare(b.name);
  });
  return arr;
}
/* 群組標題列 HTML（colspan 跨整表）。cols=表格欄數。 */
function groupHdrRow(g, cols){
  return `<tr class="grouphdr ${g.isConcept?'gc':'gi'}"><td colspan="${cols}"><span class="ghlbl"><span class="gchip">${g.isConcept?'概念':'產業'}</span>${g.name}<span class="gcount">${g.rows.length}檔</span></span></td></tr>`;
}

/* ---------- 分頁切換 ---------- */
const PANES = ["home", "screen", "trust", "flow"];
document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("on")); t.classList.add("on");
  const id=t.dataset.tab;
  PANES.forEach(p=>document.getElementById("tab-"+p).classList.toggle("hidden", p!==id));
  if(id==="flow") loadIndustry();   // 進資金流向分頁才載入熱圖／資金輪動（延遲載入）
}));

/* ---------- 首頁：市場回撤卡 ---------- */
function renderDD(){
  const order=["TWII","SOX","KOSPI","TSMC"];
  const nm={TWII:"台股加權指數",SOX:"費城半導體 SOX",KOSPI:"韓國 KOSPI",TSMC:"台積電 2330"};
  document.getElementById("ddcards").innerHTML=order.map(k=>{
    const m=MARKET?MARKET[k]:null;
    if(!m) return `<div class="ddcard"><div class="ddname">${nm[k]||k}</div><div class="ddna">資料暫時無法取得</div></div>`;
    const fmt=(v)=> m.kind==="price" ? v.toFixed(2) : Math.round(v).toLocaleString();
    const ddAbs=Math.abs(m.dd).toFixed(2);
    const flat=Math.abs(m.dd)<0.05;
    const barW=Math.min(Math.abs(m.dd),50)/50*100;
    const dbnote=m.db_only?' <span style="color:var(--dim);font-size:10px">(資料庫區間)</span>':'';
    return `<div class="ddcard">
      <div class="ddname">${m.name}</div>
      <div class="ddbig">距歷史高點 <b class="${flat?'flat':''}">${flat?'≈ 0':'−'+ddAbs}%</b></div>
      <div class="ddbar"><div class="ddbarfill" style="width:${barW}%"></div></div>
      <div class="ddrow"><span class="k">歷史最高價${dbnote}</span><span class="v">${fmt(m.ath)}</span><span class="d">${m.ath_date}</span></div>
      <div class="ddrow"><span class="k">最近收盤</span><span class="v">${fmt(m.last)}</span><span class="d">${m.last_date}</span></div>
    </div>`;
  }).join("");
}

/* ---------- 首頁：台股波動率（加權指數年化歷史波動率）---------- */
function renderVol(){
  const box=document.getElementById("volwrap"); if(!box) return;
  const m=MARKET?MARKET.TWII:null;
  const v=(m&&m.vol)?m.vol:null;
  if(!v||v.hv20==null){ box.innerHTML=""; return; }
  const p=(v.pct1y!=null)?v.pct1y:null;
  // 波動狀態：優先用「近一年百分位」判定（會隨市場自我校準）；無百分位時退回用絕對值粗分。
  const basis = p!=null ? p : (v.hv20>=25?85:v.hv20>=18?55:v.hv20>=13?35:15);
  let lvl,col;
  if(basis>=80){lvl="高波動";col="var(--up)";}
  else if(basis>=55){lvl="偏高";col="var(--amber)";}
  else if(basis>=30){lvl="中性";col="var(--muted)";}
  else{lvl="低波動";col="var(--down)";}
  const barW=Math.max(2,Math.min(v.hv20,50)/50*100);
  const sub=[];
  if(v.hv60!=null) sub.push(`60日 <b>${v.hv60.toFixed(1)}%</b>`);
  if(p!=null) sub.push(`近一年位階 <b>${p}%</b>`);
  box.innerHTML=`<div class="volcard">
    <div class="vt">台股波動率 <span class="vd">加權指數・年化・${m.last_date||""}</span></div>
    <div class="vmain">
      <span class="vbig" style="color:${col}">${v.hv20.toFixed(1)}<span style="font-size:16px;color:var(--dim);font-weight:700"> %</span></span>
      <span class="vlvl" style="color:${col}">${lvl}</span>
    </div>
    <div class="volbar"><div class="volbarfill" style="width:${barW}%;background:${col}"></div></div>
    <div class="vsub">近20日 年化波動率${sub.length?" ・ "+sub.join(" ・ "):""}</div>
  </div>`;
}

/* ⑦ 法人動向：三大法人 / 外資台指期 / 融資融券 */
function renderExtras(){
  const box=document.getElementById("flowwrap"); if(!box) return;
  const E=EXTRAS||{}, i3=E.inst3, mg=E.margin, tx=E.txf_foreign;
  if(!i3 && !mg && !tx){ box.innerHTML=""; return; }
  const r1=(v)=> Math.round(v*10)/10;
  const sign=(v)=> v==null?"—":(v>0?"+":"")+r1(v).toLocaleString();
  const sgI=(v)=> v==null?"—":(v>0?"+":"")+Math.round(v).toLocaleString();
  const col=(v)=> v==null?"var(--dim)":(v>0?"var(--up)":(v<0?"var(--down)":"var(--text)"));
  const cards=[];
  if(i3){
    cards.push(`<div class="fcard"><div class="ft">三大法人買賣超 <span class="fd">${i3.date||""}</span></div>
      <div class="fv" style="color:${col(i3.total)}">${sign(i3.total)}<span class="fu"> 億</span></div>
      <div class="fsub">外資 <b style="color:${col(i3.foreign)}">${sign(i3.foreign)}</b> ・ 投信 <b style="color:${col(i3.trust)}">${sign(i3.trust)}</b> ・ 自營 <b style="color:${col(i3.dealer)}">${sign(i3.dealer)}</b></div></div>`);
  }
  if(tx){
    const tag=tx.net_oi==null?"":(tx.net_oi>0?" 淨多":" 淨空");
    const det=(tx.long_oi!=null&&tx.short_oi!=null)?`多單 ${Math.round(tx.long_oi).toLocaleString()} ・ 空單 ${Math.round(tx.short_oi).toLocaleString()} 口`:"未平倉口數（負=淨空）";
    cards.push(`<div class="fcard"><div class="ft">外資台指期未平倉 <span class="fd">${tx.date||""}</span></div>
      <div class="fv" style="color:${col(tx.net_oi)}">${sgI(tx.net_oi)}<span class="fu"> 口${tag}</span></div>
      <div class="fsub">${det}</div></div>`);
  }
  if(mg){
    const fu=mg.fin_unit||"億";
    cards.push(`<div class="fcard"><div class="ft">融資融券餘額 <span class="fd">${mg.date||""}</span></div>
      <div class="fv">融資 ${mg.fin_bal!=null?Math.round(mg.fin_bal).toLocaleString():"—"}<span class="fu"> ${fu}</span> <b style="color:${col(mg.fin_chg)};font-size:14px">${mg.fin_chg!=null?"("+sgI(mg.fin_chg)+")":""}</b></div>
      <div class="fsub">融券 ${mg.short_bal!=null?Math.round(mg.short_bal).toLocaleString():"—"} 張 <b style="color:${col(mg.short_chg)}">${mg.short_chg!=null?"("+sgI(mg.short_chg)+")":""}</b></div></div>`);
  }
  box.innerHTML=`<div class="flowtitle">法人動向（最近交易日）</div><div class="flowgrid">${cards.join("")}</div>`;
}

/* 資金流向 TOP10：大戶(三大法人)/投信 × 當日/近5/20/60日 */
let flowKey="big_d";
function renderFlows(){
  const root=document.getElementById("toptop"); if(!root) return;
  const F=FLOWS||{};
  const dd=document.getElementById("flowdate"); if(dd) dd.textContent=F.date||"—";
  const grp=F[flowKey];
  if(!grp || ((!grp.buy||!grp.buy.length)&&(!grp.sell||!grp.sell.length))){
    root.innerHTML=`<div class="tpanel"><div class="tpempty">資料準備中：三大法人(T86)資料抓到後即顯示</div></div>`; return;
  }
  const maxAbs=(arr)=> Math.max(1,...arr.map(x=>Math.abs(x.amt)));
  const panel=(title,cls,arr)=>{
    const mx=maxAbs(arr);
    const rows=(arr||[]).map((x,i)=>{
      const w=Math.max(2,Math.abs(x.amt)/mx*100);
      const cg=x.chg==null?"":(x.chg>0?"+":"")+x.chg.toFixed(2)+"%";
      const cgc=x.chg==null?"var(--dim)":(x.chg>0?"var(--up)":(x.chg<0?"var(--down)":"var(--muted)"));
      const av=(x.amt>0?"+":"")+x.amt.toLocaleString();
      const avc=cls==="buy"?"var(--up)":"var(--down)";
      return `<div class="frow ${cls}"><span class="fbar" style="width:${w}%"></span>
        <span class="frk">${i+1}</span>
        <span class="fnm2" onclick="openChart('${x.sid}')"><span class="fnmtxt">${x.name||x.sid}<i>${x.sid}</i></span>${indTag(x.sid)}</span>
        <span class="fval" style="color:${avc}">${av}<small>億</small></span>
        <span class="fcg" style="color:${cgc}">${cg}</span></div>`;
    }).join("")||`<div class="tpempty">無</div>`;
    return `<div class="tpanel"><div class="tphd ${cls}">${title}</div>${rows}</div>`;
  };
  root.innerHTML=panel("買超 TOP10","buy",grp.buy)+panel("賣超 TOP10","sell",grp.sell);
}
(function(){ const c=document.getElementById("fchips"); if(!c) return;
  c.querySelectorAll(".fchip").forEach(b=>b.addEventListener("click",()=>{
    c.querySelectorAll(".fchip").forEach(x=>x.classList.remove("on")); b.classList.add("on");
    flowKey=b.dataset.fk; renderFlows();
  }));
})();

/* ===== 市場熱圖 + 120日產業資金輪動（延遲載入 data/industry.json） ===== */
let INDDATA=null, INDLOAD=false;
async function loadIndustry(){
  if(INDDATA){ renderHeatmap(); return; }
  if(INDLOAD) return; INDLOAD=true;
  try{
    const r=await fetch(`data/industry.json?v=${BUILD_V}`,{cache:"default"});
    if(r.ok) INDDATA=await r.json();
  }catch(e){}
  INDLOAD=false;
  if(!INDDATA){
    const hb=document.getElementById("heatbox"); if(hb) hb.innerHTML='<div class="tpempty">熱圖資料尚未產生（下次自動更新後出現）</div>';
    const rb=document.getElementById("rotbox"); if(rb) rb.innerHTML='<div class="tpempty">資金輪動資料尚未產生（下次自動更新後出現）</div>';
    return;
  }
  const hd=document.getElementById("heatdate"); if(hd&&INDDATA.heatmap) hd.textContent=INDDATA.heatmap.date||"—";
  const rd=document.getElementById("rotdays"); if(rd&&INDDATA.rotation) rd.textContent=INDDATA.rotation.win_days||120;
  renderHeatmap(); renderRotation();
}
document.querySelectorAll("[data-rm]").forEach(b=>b.addEventListener("click",()=>{
  rotMode=b.dataset.rm;
  document.querySelectorAll("[data-rm]").forEach(x=>x.classList.toggle("on",x.dataset.rm===rotMode));
  const rd=document.getElementById("rotdays"), rot=curRot(); if(rd&&rot) rd.textContent=(rot.win_days||120);
  renderRotation();
}));

/* 漲跌幅 → 紅(漲)/綠(跌) 熱圖色，深淺隨幅度 */
function heatColor(chg){
  if(chg==null||isNaN(chg)) return "rgba(120,120,130,.28)";
  const v=Math.max(-9.5,Math.min(9.5,Number(chg)));
  if(v>0.05){ return `rgba(251,59,65,${(0.16+0.66*Math.min(1,v/7)).toFixed(3)})`; }
  if(v<-0.05){ return `rgba(30,199,122,${(0.16+0.66*Math.min(1,-v/7)).toFixed(3)})`; }
  return "rgba(120,120,130,.3)";
}

/* 平方化樹狀圖(squarified treemap)：nodes 各含 value，回傳對應 {x,y,w,h}（依輸入順序） */
function squarify(nodes, x, y, w, h){
  const res=new Array(nodes.length).fill(null);
  const items=nodes.map((d,i)=>({v:Math.max(0,d.value)||0,i})).filter(d=>d.v>0).sort((a,b)=>b.v-a.v);
  const total=items.reduce((s,d)=>s+d.v,0);
  if(total<=0||w<=0||h<=0) return res;
  const scale=(w*h)/total; items.forEach(d=>d.a=d.v*scale);
  let ar={x,y,w,h};
  const worst=(row,len)=>{ const s=row.reduce((a,b)=>a+b.a,0), mx=Math.max(...row.map(d=>d.a)), mn=Math.min(...row.map(d=>d.a));
    return Math.max(len*len*mx/(s*s), s*s/(len*len*mn)); };
  const place=(row,len,vert)=>{ const s=row.reduce((a,b)=>a+b.a,0), th=s/len; let p=vert?ar.y:ar.x;
    row.forEach(d=>{ const cl=d.a/th; res[d.i]= vert? {x:ar.x,y:p,w:th,h:cl} : {x:p,y:ar.y,w:cl,h:th}; p+=cl; });
    if(vert){ ar.x+=th; ar.w-=th; } else { ar.y+=th; ar.h-=th; } };
  let row=[], i=0;
  while(i<items.length){
    const vert=ar.w>=ar.h, len=vert?ar.h:ar.w, n=items[i];
    if(!row.length){ row.push(n); i++; continue; }
    if(worst(row.concat([n]),len)<=worst(row,len)){ row.push(n); i++; }
    else { place(row,len,vert); row=[]; }
  }
  if(row.length){ const vert=ar.w>=ar.h, len=vert?ar.h:ar.w; place(row,len,vert); }
  return res;
}

function renderHeatmap(){
  const box=document.getElementById("heatbox"); if(!box||!INDDATA||!INDDATA.heatmap) return;
  const W=box.clientWidth, H=box.clientHeight;
  if(W<10||H<10){ requestAnimationFrame(renderHeatmap); return; }   // 分頁剛顯示、尺寸未定時重試
  const secs=(INDDATA.heatmap.sectors||[]).filter(s=>s.stocks&&s.stocks.length);
  if(!secs.length){ box.innerHTML='<div class="tpempty">無熱圖資料</div>'; return; }
  const srects=squarify(secs.map(s=>({value:s.turn})),0,0,W,H);
  let html="";
  secs.forEach((s,si)=>{
    const R=srects[si]; if(!R||R.w<2||R.h<2) return;
    const lab=(R.h>26&&R.w>40);
    const padTop=lab?13:0;
    html+=`<div class="heatsec" style="left:${R.x}px;top:${R.y}px;width:${R.w}px;height:${R.h}px">`;
    if(lab) html+=`<div class="hsl">${s.name} ${s.turn>=1?Math.round(s.turn):s.turn.toFixed(1)}億</div>`;
    const trects=squarify(s.stocks.map(x=>({value:x.turn})),0,padTop,R.w,Math.max(1,R.h-padTop));
    s.stocks.forEach((st,ti)=>{
      const t=trects[ti]; if(!t||t.w<1||t.h<1) return;
      const big=t.w>=44&&t.h>=30, tiny=t.w<26||t.h<18;
      const fs=Math.max(8,Math.min(13,Math.round(Math.min(t.w/4.2,t.h/2.6))));
      const cg=(st.chg==null)?"":(st.chg>0?"+":"")+Number(st.chg).toFixed(2)+"%";
      html+=`<div class="htile${tiny?' tiny':''}" style="left:${t.x}px;top:${t.y}px;width:${t.w}px;height:${t.h}px;background:${heatColor(st.chg)}" onclick="openChart('${st.sid}')">`;
      if(!tiny) html+=`<span class="hn" style="font-size:${fs}px">${st.name||st.sid}</span>`;
      if(big) html+=`<span class="hc" style="font-size:${Math.max(8,fs-2)}px">${cg}</span>`;
      html+=`</div>`;
    });
    html+=`</div>`;
  });
  box.innerHTML=html;
}
let _heatTO=null;
window.addEventListener("resize",()=>{ if(!INDDATA) return; clearTimeout(_heatTO); _heatTO=setTimeout(renderHeatmap,180); });

/* ---- 120日產業資金輪動：發散長條 + 點族群展開成分股 ---- */
function fnum(v,d){ return (v==null||isNaN(v))?"—":(Number(v)>0?"+":"")+Number(v).toFixed(d==null?1:d); }
function gpx(p){ return (p==null||isNaN(p))?"—":Number(p).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}); }
function gcls(v){ return (v==null||isNaN(v))?"dim":(Number(v)>0?"up":(Number(v)<0?"down":"")); }
let rotMode="ind";   // ind=產業鏈輪動 / concept=概念股輪動
function curRot(){ if(!INDDATA) return null; return rotMode==="concept" ? (INDDATA.rotation_concept||null) : (INDDATA.rotation||null); }
function renderRotation(){
  const box=document.getElementById("rotbox"); if(!box||!INDDATA) return;
  const rot=curRot();
  const gs=(rot&&rot.groups)||[];
  if(!gs.length){ box.innerHTML='<div class="tpempty">'+(rotMode==="concept"?"概念股資金輪動資料尚未產生（下次更新後出現）":"無資金輪動資料")+'</div>'; return; }
  const mx=Math.max(1,...gs.map(g=>Math.abs(g.net120||0)));
  box.innerHTML=gs.map((g,i)=>{
    const v=g.net120||0, w=(Math.abs(v)/mx*50).toFixed(2), pos=v>=0;
    const vc=pos?"var(--up)":"var(--down)";
    const m=g.net20||0, mc=m>0?"var(--up)":(m<0?"var(--down)":"var(--dim)"), ar=m>0?"▲":(m<0?"▼":"—");
    return `<div class="rotrow" data-i="${i}">
      <div class="rothead" onclick="toggleGroup(${i})">
        <div class="rotleft">
          <div class="rotnm"><span class="rcar">▶</span>${g.name}</div>
          <div class="rotmeta">${g.sector||""} ・ ${g.n}檔</div>
        </div>
        <div class="rotbarwrap"><div class="rotaxis"></div>
          <div class="rotbar ${pos?'pos':'neg'}" style="width:${w}%"></div></div>
        <div style="flex:0 0 78px;text-align:right">
          <div style="font-size:13px;font-weight:800;font-variant-numeric:tabular-nums;color:${vc}">${fnum(v)}<small style="font-size:9px;color:var(--dim)">億</small></div>
          <div style="font-size:10px;color:${mc};font-variant-numeric:tabular-nums">${ar}${fnum(Math.abs(m))}<span style="color:var(--dim)">·20日</span></div>
        </div>
      </div>
      <div class="rotpanel" id="gpanel-${i}"></div>
    </div>`;
  }).join("");
}
const GCOLS=[
  ["股價","close"],["漲幅","chg"],["位階","wj"],["月斜","yx"],
  ["主5","z5"],["主10","z10"],["法20","net20"],["量比","vr"],["季乖離","bias60"]
];
function gcell(key,r){
  const v=r[key];
  if(v==null||isNaN(v)) return '<span class="cv dim">—</span>';
  const n=Number(v);
  switch(key){
    case "close": return `<span class="cv">${gpx(n)}</span>`;
    case "chg":   return `<span class="cv ${gcls(n)}">${(n>0?"+":"")+n.toFixed(2)}%</span>`;
    case "wj":    return `<span class="cv">${Math.round(n)}</span>`;
    case "yx": case "z5": case "z10": case "bias60":
      return `<span class="cv ${gcls(n)}">${(n>0?"+":"")+n.toFixed(1)}%</span>`;
    case "net20": return `<span class="cv ${gcls(n)}">${fnum(n)}</span>`;
    case "vr":    return `<span class="cv">${n.toFixed(2)}x</span>`;
  }
  return `<span class="cv">${n}</span>`;
}
function toggleGroup(i){
  const row=document.querySelector(`.rotrow[data-i="${i}"]`); if(!row) return;
  const open=row.classList.toggle("open");
  const panel=document.getElementById("gpanel-"+i);
  if(open && panel && !panel.dataset.done){
    const g=((curRot()&&curRot().groups)||[])[i]; if(!g){ return; }
    let head=`<th class="frz">名稱<br><span style="font-weight:500;color:var(--dim)">代號·市場</span></th>`;
    // 每欄堆兩個指標，與成分股緊湊表一致
    const pairs=[["股價","漲幅"],["位階","月斜"],["主5","主10"],["法20","量比"],["季乖離",""]];
    const keymap={"股價":"close","漲幅":"chg","位階":"wj","月斜":"yx","主5":"z5","主10":"z10","法20":"net20","量比":"vr","季乖離":"bias60"};
    pairs.forEach(([a,b])=>{ head+=`<th>${a}${b?'<br>'+b:''}</th>`; });
    const body=(g.stocks||[]).map(r=>{
      const sc=(r.chg==null)?"":(r.chg>0?"side-up":(r.chg<0?"side-down":""));
      let tds="";
      pairs.forEach(([a,b])=>{ tds+=`<td>${gcell(keymap[a],r)}${b?`<br>${gcell(keymap[b],r)}`:""}</td>`; });
      return `<tr class="${sc}" onclick="openChart('${r.sid}')">
        <td class="frz"><div class="gnm">${r.name||r.sid}</div><div class="gsub">${r.sid}${r.mkt?" "+r.mkt:""}</div></td>${tds}</tr>`;
    }).join("");
    panel.innerHTML=`<div class="gtbl-wrap"><table class="gtbl"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
    panel.dataset.done="1";
  }
}
let trustThr = 50;
let trustSort = {key:"評分", asc:false};
let trustGroup = true;
const SELL_BACK_FRAC = 0.6;   // 連買後若被賣回 ≥ 此比例的累計買超 → 視為投信已落跑，排除
const TCOLS = [
  ["代號","l",        r=>r.sid],
  ["名稱","l",        r=>r.name],
  ["市場","",         r=>r.market],
  ["連買(天)","",     r=>r.days],
  ["投信買超佔比","", r=>r.dominance],
  ["連買累計(張)","", r=>r.total],
  ["現價","",         r=>r.lastClose],
  ["投信成本","",     r=>r.cost],
  ["距成本%","",      r=>r.costBias],
  ["連買最高","",     r=>r.hi],
  ["距高點%","",      r=>r.gapHigh],
  ["連買漲幅%","",    r=>r.streakRet],
  ["賣回%","",        r=>r.soldBack],
  ["仍在買","",       r=>r.stillBuying?1:0],
  ["評分","",         r=>r.score],
];
function computeTrustRows(thr){
  const data = (TRUST && TRUST.data) ? TRUST.data : {};
  const minStreak = (TRUST && TRUST.min_streak) ? TRUST.min_streak : 3;
  const rows = [];
  for(const sid in data){
    const o = data[sid]; const s = o.series;
    if(!s || s.length < minStreak) continue;
    let a=-1, b=-1, end=s.length-1;
    while(end>=0){
      if(s[end][1] >= thr){
        let start=end; while(start-1>=0 && s[start-1][1]>=thr) start--;
        if(end-start+1 >= minStreak){ a=start; b=end; break; }
        end=start-1;
      } else end--;
    }
    if(a<0) continue;
    let total=0, vol=0, hi=-Infinity, cN=0, cD=0;
    for(let k=a;k<=b;k++){ const e=s[k]; total+=e[1]; vol+=e[4]; hi=Math.max(hi,e[3]); if(e[1]>0){cN+=e[2]*e[1]; cD+=e[1];} }
    const cost = cD>0 ? cN/cD : s[b][2];
    const last = s[s.length-1]; const lastClose = last[2];
    const base = a>0 ? s[a-1][2] : s[a][2];
    const streakRet = base>0 ? (s[b][2]/base - 1) : 0;
    if(!(lastClose <= hi || lastClose < cost)) continue;
    // ③ 連買後被賣回多少（投信是否已落跑）
    let postNet=0; for(let k=b+1;k<s.length;k++) postNet+=s[k][1];
    const soldBack = total>0 ? Math.max(0, -postNet)/total : 0;   // 賣回佔累計買超比例
    if(soldBack >= SELL_BACK_FRAC) continue;                       // 同等/接近量賣回 → 排除
    const dominance = vol>0 ? total/vol : 0;
    const gapHigh = lastClose>0 ? (hi/lastClose - 1) : 0;
    const costBias = cost>0 ? (lastClose/cost - 1) : 0;
    const days = b-a+1;
    const stillBuying = (b === s.length-1);
    const sc_dom  = Math.min(Math.max(dominance/0.25, 0), 1);
    const sc_acc  = Math.min(days/7, 1)*0.4 + Math.min(total/3000, 1)*0.6;
    const sc_lag  = Math.min(Math.max(gapHigh/0.20, 0), 1);
    const sc_cost = costBias<=0 ? 1 : Math.max(1 - costBias/0.10, 0);
    let score = 100*(0.35*sc_dom + 0.20*sc_acc + 0.25*sc_lag + 0.20*sc_cost);
    if(stillBuying) score += 5;
    if(streakRet > 0.20) score -= 10;
    score -= soldBack*15;                                          // 有被部分賣回 → 扣分
    score = Math.max(0, Math.min(100, score));
    rows.push({sid, name:o.name, market:o.market, days, dominance, total, lastClose, cost, costBias, hi, gapHigh, streakRet, soldBack, stillBuying, score});
  }
  // ② 依目前選擇的欄位排序
  const acc = (TCOLS.find(c=>c[0]===trustSort.key)||TCOLS[TCOLS.length-1])[2];
  rows.sort((x,y)=>{
    const xv=acc(x), yv=acc(y);
    if(typeof xv==="string"||typeof yv==="string"){
      const r=String(xv).localeCompare(String(yv)); return trustSort.asc?r:-r;
    }
    return trustSort.asc ? xv-yv : yv-xv;
  });
  return rows;
}
function renderTrust(){
  document.getElementById("trustdate").textContent = (TRUST && TRUST.date) ? TRUST.date : "—";
  const head=document.getElementById("trusthead");
  head.innerHTML = TCOLS.map(([n,c])=>{const ar=trustSort.key===n?`<span class="ar">${trustSort.asc?"▲":"▼"}</span>`:""; return `<th class="${c}" data-tk="${n}">${n}${ar}</th>`;}).join("");
  head.querySelectorAll("th").forEach(th=>th.onclick=()=>{const k=th.dataset.tk; if(trustSort.key===k)trustSort.asc=!trustSort.asc; else {trustSort.key=k; trustSort.asc=false;} renderTrust();});
  const tb=document.getElementById("trustbody");
  const nc=TCOLS.length;
  if(!TRUST || !TRUST.data || !Object.keys(TRUST.data).length){
    tb.innerHTML=`<tr><td colspan="${nc}" style="text-align:center;color:var(--dim);padding:36px">投信資料準備中（下次自動更新後出現）</td></tr>`; return;
  }
  const rows = computeTrustRows(trustThr);
  if(!rows.length){
    tb.innerHTML=`<tr><td colspan="${nc}" style="text-align:center;color:var(--dim);padding:36px">此門檻下沒有符合「投信連買 ≥3 日且尚未漲上去、且未被賣回」的個股<br>可試試降低每日張數門檻</td></tr>`; return;
  }
  tb.innerHTML = trustGroup
    ? groupByConcept(rows, r=>r.sid).map(g=>groupHdrRow(g,nc)+g.rows.map(trustRowHtml).join("")).join("")
    : rows.map(trustRowHtml).join("");
}
function trustRowHtml(r){
    const pct=(v)=>(v>=0?"+":"")+(v*100).toFixed(1)+"%";
    const domc = r.dominance>=0.25?"var(--amber)":r.dominance>=0.12?"#d98818":"var(--text)";
    const costc = r.costBias<=0?"var(--down)":"var(--up)";
    const sbc = r.soldBack>=0.3?"var(--amber)":"var(--dim)";
    const scC = r.score>=70?"var(--up)":r.score>=45?"var(--amber)":"var(--dim)";
    const mkt = r.market==="上市"?"twse":"tpex";
    const has = `onclick="openChart('${r.sid}')"`;
    return `<tr>
      <td class="l"><span class="code">${r.sid}</span></td>
      <td class="l"><span class="nm" ${has}>${r.name||""}</span>${indTag(r.sid)}</td>
      <td><span class="mkt ${mkt}">${r.market}</span></td>
      <td class="num" style="font-weight:700">${r.days}</td>
      <td class="num"><b style="color:${domc}">${(r.dominance*100).toFixed(1)}%</b></td>
      <td class="num">${Math.round(r.total).toLocaleString()}</td>
      <td class="num" style="font-weight:700">${r.lastClose.toFixed(2)}</td>
      <td class="num" style="color:var(--muted)">${r.cost.toFixed(2)}</td>
      <td class="num" style="color:${costc};font-weight:700">${pct(r.costBias)}</td>
      <td class="num" style="color:var(--muted)">${r.hi.toFixed(2)}</td>
      <td class="num" style="color:var(--amber)">${pct(r.gapHigh)}</td>
      <td class="num">${pct(r.streakRet)}</td>
      <td class="num" style="color:${sbc}">${(r.soldBack*100).toFixed(0)}%</td>
      <td class="num">${r.stillBuying?'<span style="color:var(--up)">是</span>':'<span style="color:var(--dim)">—</span>'}</td>
      <td><span class="scorewrap"><span class="scoretrack"><span class="scorefill" style="width:${Math.min(r.score,100)}%;background:${scC}"></span></span><span class="scoreval" style="color:${scC}">${r.score.toFixed(0)}</span></span></td>
    </tr>`;
}
document.querySelectorAll(".thrbtn").forEach(b=>b.addEventListener("click",()=>{
  document.querySelectorAll(".thrbtn").forEach(x=>x.classList.remove("on")); b.classList.add("on");
  trustThr=parseInt(b.dataset.thr,10); renderTrust();
}));
document.getElementById("trustGtog").addEventListener("click",e=>{ trustGroup=!trustGroup; e.currentTarget.classList.toggle("on",trustGroup); renderTrust();});

/* ---------- 選股表格 ---------- */
const TAGS = {
  "突破季高":["rgba(245,165,36,.13)","#f7c14b","rgba(245,165,36,.35)"],
  "月線翻揚":["rgba(34,197,94,.12)","#56d97e","rgba(34,197,94,.3)"],
  "站上季線":["rgba(77,159,255,.12)","#6fb0ff","rgba(77,159,255,.3)"],
  "季線翻揚":["rgba(6,182,212,.12)","#34d3e6","rgba(6,182,212,.3)"],
  "多頭排列":["rgba(183,148,255,.12)","#c9acff","rgba(183,148,255,.3)"],
  "站上年線":["rgba(255,99,132,.12)","#ff9aa8","rgba(255,99,132,.3)"],
};
const COLS = [["代號","l"],["名稱","l"],["市場",""],["收盤",""],["漲跌%",""],
  ["成交量(張)",""],["月均量(張)",""],["量比",""],["5日量/月量",""],["季線乖離%",""],["評分",""],["爆量月位階",""],["強度標記","l"]];
const num = v => { if(v==null||v==="") return null; const n=parseFloat(String(v).replace(/,/g,"")); return isNaN(n)?null:n; };
const fmtInt = v => { const n=num(v); return n==null?"":n.toLocaleString(); };
let state = { sort:"評分", asc:false, mkt:"全部", q:"" };

function view(){
  let d = (RESULTS||[]).filter(r=>r["代號"]);
  if(state.mkt!=="全部") d=d.filter(r=>r["市場"]===state.mkt);
  if(state.q){const q=state.q.toLowerCase(); d=d.filter(r=>(r["代號"]||"").toLowerCase().includes(q)||(r["名稱"]||"").toLowerCase().includes(q));}
  d.sort((a,b)=>{const x=num(a[state.sort]),y=num(b[state.sort]); if(x==null&&y==null)return 0; if(x==null)return 1; if(y==null)return -1; return state.asc?x-y:y-x;});
  return d;
}
function renderCards(d){
  const sc=d.map(r=>num(r["評分"])).filter(v=>v!=null), vr=d.map(r=>num(r["量比"])).filter(v=>v!=null);
  const twse=d.filter(r=>r["市場"]==="上市").length, tpex=d.filter(r=>r["市場"]==="上櫃").length;
  const avg=sc.length?(sc.reduce((a,b)=>a+b,0)/sc.length).toFixed(1):"—", mv=vr.length?Math.max(...vr).toFixed(1):"—", lim=d.filter(r=>num(r["漲跌%"])>=9.5).length;
  document.getElementById("cards").innerHTML=`
    <div class="stat"><div class="l">入選總數</div><div class="v" style="color:var(--amber)">${d.length}</div><div class="s">上市 ${twse} ・ 上櫃 ${tpex}</div></div>
    <div class="stat"><div class="l">平均評分</div><div class="v" style="color:var(--blue)">${avg}</div><div class="s">滿分 100</div></div>
    <div class="stat"><div class="l">最高量比</div><div class="v" style="color:var(--amber)">${mv}x</div><div class="s">今日量 ÷ 月均量</div></div>
    <div class="stat"><div class="l">今日漲停</div><div class="v" style="color:var(--up)">${lim}</div><div class="s">漲幅 ≥ 9.5%</div></div>`;
}
function renderBars(d){
  const top=[...d].sort((a,b)=>(num(b["量比"])||0)-(num(a["量比"])||0)).slice(0,20), max=top.length?Math.max(...top.map(r=>num(r["量比"])||0)):1;
  document.getElementById("bars").innerHTML=top.map(r=>{const v=num(r["量比"])||0,w=Math.max(v/max*100,2),c=v>=3?"var(--amber)":v>=2?"#d98818":"var(--dim)";
    return `<div class="barrow"><div class="lbl">${r["代號"]} ${r["名稱"]||""}</div><div class="bartrack"><div class="barfill" style="width:${w}%;background:${c}"></div></div><div class="barval" style="color:${c}">${v.toFixed(2)}x</div></div>`;}).join("")||'<div style="color:var(--dim);font-size:12px;padding:8px 0">今日無資料</div>';
}
function renderHead(){
  document.getElementById("thead").innerHTML=COLS.map(([n,c])=>{const ar=state.sort===n?`<span class="ar">${state.asc?"▲":"▼"}</span>`:""; return `<th class="${c}" data-k="${n}">${n}${ar}</th>`;}).join("");
  document.querySelectorAll("#thead th").forEach(th=>th.onclick=()=>{const k=th.dataset.k; if(state.sort===k)state.asc=!state.asc; else {state.sort=k;state.asc=false;} render();});
}
function screenRowHtml(r){
    const chg=num(r["漲跌%"]), cc=chg>0?"var(--up)":chg<0?"var(--down)":"var(--muted)";
    const lim=chg>=9.5?`<span class="lim" style="background:var(--up)">漲停</span>`:(chg<=-9.5?`<span class="lim" style="background:var(--down)">跌停</span>`:"");
    const vr=num(r["量比"])||0, vc=vr>=3?"var(--amber)":vr>=2?"#d98818":"var(--text)";
    const sv=num(r["評分"])||0, scC=sv>=70?"var(--up)":sv>=45?"var(--amber)":"var(--dim)", mkt=r["市場"]==="上市"?"twse":"tpex";
    const has=`onclick="openChart('${r["代號"]}')"`;
    const tags=(r["強度標記"]||"").split("·").filter(Boolean).map(t=>{const c=TAGS[t]||["rgba(94,111,134,.15)","#93a3b8","rgba(94,111,134,.3)"]; return `<span class="tag" style="background:${c[0]};color:${c[1]};border-color:${c[2]}">${t}</span>`;}).join("");
    return `<tr><td class="l"><span class="code">${r["代號"]}</span></td><td class="l"><span class="nm" ${has}>${r["名稱"]||""}</span>${indTag(r["代號"])}</td>
      <td><span class="mkt ${mkt}">${r["市場"]}</span></td><td class="num">${r["收盤"]}</td>
      <td class="num" style="color:${cc};font-weight:700">${chg>0?"+":""}${r["漲跌%"]}%${lim}</td>
      <td class="num">${fmtInt(r["成交量(張)"])}</td><td class="num" style="color:var(--muted)">${fmtInt(r["月均量(張)"])}</td>
      <td class="num"><span class="vr" style="color:${vc}">${r["量比"]}x</span></td><td class="num">${r["5日量/月量"]}</td>
      <td class="num">${r["季線乖離%"]}%</td>
      <td><span class="scorewrap"><span class="scoretrack"><span class="scorefill" style="width:${Math.min(sv,100)}%;background:${scC}"></span></span><span class="scoreval" style="color:${scC}">${r["評分"]}</span></span></td>
      <td class="num">${r["_zoneLabel"]?`<span class="zone ${r["_zoneCls"]}">${r["_zoneLabel"]}</span><span class="zpos">${r["爆量月位階"]}%</span>`:'<span style="color:var(--dim)">—</span>'}</td>
      <td class="tags">${tags}</td></tr>`;
}
let screenGroup=true;
function renderTable(d){
  const tb=document.getElementById("tbody");
  if(!d.length){ tb.innerHTML=`<tr><td colspan="13" style="text-align:center;color:var(--dim);padding:36px">今日無符合條件的標的</td></tr>`; return; }
  if(!screenGroup){ tb.innerHTML=d.map(screenRowHtml).join(""); return; }
  tb.innerHTML=groupByConcept(d, r=>r["代號"]).map(g=>groupHdrRow(g,13)+g.rows.map(screenRowHtml).join("")).join("");
}
function render(){const d=view(); renderCards(d); renderBars(d); renderHead(); renderTable(d);
  document.getElementById("subtitle").textContent=`資料日 __DATE__ ・ 顯示 ${d.length} 檔`; syncTopScroll();}
// 列表上方的水平捲動bar 與表格同步
function syncTopScroll(){
  const wrap=document.getElementById("screenWrap"), bar=document.getElementById("screenHbar");
  if(!wrap||!bar) return; const inner=bar.firstElementChild, tbl=wrap.querySelector("table"); if(!tbl) return;
  inner.style.width=tbl.scrollWidth+"px";
  let lock=false;
  bar.onscroll=()=>{ if(lock)return; lock=true; wrap.scrollLeft=bar.scrollLeft; lock=false; };
  wrap.onscroll=()=>{ if(lock)return; lock=true; bar.scrollLeft=wrap.scrollLeft; lock=false; };
}
window.addEventListener("resize",()=>{ try{ syncTopScroll(); }catch(e){} });
document.getElementById("q").addEventListener("input",e=>{state.q=e.target.value; render();});
document.querySelectorAll(".chip").forEach(c=>c.addEventListener("click",()=>{document.querySelectorAll(".chip").forEach(x=>x.classList.remove("on")); c.classList.add("on"); state.mkt=c.dataset.mkt; render();}));
document.getElementById("screenGtog").addEventListener("click",e=>{ screenGroup=!screenGroup; e.currentTarget.classList.toggle("on",screenGroup); render();});

/* ===== 技術線圖引擎 ===== */
const MACOLOR={5:"#f5c518",10:"#e23fd0",20:"#27c4dc",60:"#c79a52",240:"#3b6fe0"};
const PRICE_MAS=[5,10,20,60,240], VOL_MAS=[5,20,60];
const UP="#fb3b41", DOWN="#1ec77a", BOLL="#8a8a93";
function SMA(a,n){const o=new Array(a.length).fill(null);let s=0,cnt=0; for(let i=0;i<a.length;i++){if(a[i]==null){o[i]=null;continue;} s+=a[i];cnt++; if(i>=n&&a[i-n]!=null){s-=a[i-n];cnt--;} if(cnt>=n)o[i]=s/n;} return o;}
function EMA(a,n){const o=new Array(a.length).fill(null);const k=2/(n+1);let p=null; for(let i=0;i<a.length;i++){if(a[i]==null){o[i]=p;continue;} p=(p==null)?a[i]:a[i]*k+p*(1-k); o[i]=p;} return o;}
function STD(a,n,ma){const o=new Array(a.length).fill(null); for(let i=n-1;i<a.length;i++){if(ma[i]==null)continue;let s=0,ok=true;for(let j=i-n+1;j<=i;j++){if(a[j]==null){ok=false;break;}const d=a[j]-ma[i];s+=d*d;} if(ok)o[i]=Math.sqrt(s/n);} return o;}
function MACD(c){const e12=EMA(c,12),e26=EMA(c,26); const dif=c.map((_,i)=>(e12[i]!=null&&e26[i]!=null&&i>=25)?e12[i]-e26[i]:null);
  const dea=new Array(c.length).fill(null);const k=2/10;let p=null; for(let i=0;i<dif.length;i++){if(dif[i]==null)continue; p=(p==null)?dif[i]:dif[i]*k+p*(1-k); dea[i]=p;}
  const osc=dif.map((d,i)=>(d!=null&&dea[i]!=null)?d-dea[i]:null); return {dif,dea,osc};}
function RSI(c,n){ const o=new Array(c.length).fill(null); if(c.length<=n) return o;
  let ag=0,al=0; for(let i=1;i<=n;i++){ const ch=c[i]-c[i-1]; if(ch>=0)ag+=ch; else al-=ch; }
  ag/=n; al/=n; o[n]=(al===0)?100:100-100/(1+ag/al);
  for(let i=n+1;i<c.length;i++){ const ch=c[i]-c[i-1], g=ch>0?ch:0, l=ch<0?-ch:0;
    ag=(ag*(n-1)+g)/n; al=(al*(n-1)+l)/n; o[i]=(al===0)?100:100-100/(1+ag/al); } return o; }
function KD(bars,n,ks,ds){ const len=bars.length, kk=new Array(len).fill(null), dd=new Array(len).fill(null);
  let pk=50, pd=50; for(let i=0;i<len;i++){ if(i<n-1) continue;
    let hi=-Infinity, lo=Infinity; for(let j=i-n+1;j<=i;j++){ const b=bars[j]; if(b.h>hi)hi=b.h; if(b.l<lo)lo=b.l; }
    const rsv=(hi===lo)?50:(bars[i].c-lo)/(hi-lo)*100;
    pk=pk*(1-1/ks)+rsv*(1/ks); pd=pd*(1-1/ds)+pk*(1/ds); kk[i]=pk; dd[i]=pd; } return {k:kk,d:dd}; }
function aggregate(daily, period){
  if(period==="D") return daily.map(b=>({d:b[0],o:b[1],h:b[2],l:b[3],c:b[4],v:b[5]}));
  const key=(s)=>{ if(period==="M") return s.slice(0,7); const p=s.split("-").map(Number); const dt=new Date(Date.UTC(p[0],p[1]-1,p[2])); const day=(dt.getUTCDay()+6)%7; dt.setUTCDate(dt.getUTCDate()-day); return dt.toISOString().slice(0,10); };
  const map={}, order=[];
  for(const b of daily){ const k=key(b[0]); if(!map[k]){ map[k]={d:b[0],o:b[1],h:b[2],l:b[3],c:b[4],v:b[5]}; order.push(k);} else { const g=map[k]; g.h=Math.max(g.h,b[2]); g.l=Math.min(g.l,b[3]); g.c=b[4]; g.v+=b[5]; g.d=b[0]; } }
  return order.map(k=>map[k]);
}
let CH={ sid:null, period:"D", bars:[], ind:null, count:90, offset:0, hover:null, hoverY:null, lock:null, lockY:null,
  volMode:"vol", ts:0, t:[], tnet:[], tcum:[], macdMode:"macd", mfs:0, mf:[], mnet:[], mcum:[],
  fs:0, f:[], fnet:[], fcum:[], b4:[], big:[], iss:null };
// 副圖(中)可切換：量→投信→外資→400張大戶；下圖可切換：MACD→RSI→KD→主力
const VOL_MODES=["vol","inst","foreign","big400"], VOL_LABEL={vol:"副圖:量",inst:"副圖:投信",foreign:"副圖:外資",big400:"副圖:大戶"};
const MACD_MODES=["macd","rsi","kd","mforce"], MACD_LABEL={macd:"下圖:MACD",rsi:"下圖:RSI",kd:"下圖:KD",mforce:"下圖:主力"};
function computeInd(bars){
  const close=bars.map(b=>b.c), vol=bars.map(b=>b.v);
  const ma={}; PRICE_MAS.forEach(n=>ma[n]=SMA(close,n));
  const mid=SMA(close,20), sd=STD(close,20,mid);
  const bu=mid.map((m,i)=>(m!=null&&sd[i]!=null)?m+2*sd[i]:null), bl=mid.map((m,i)=>(m!=null&&sd[i]!=null)?m-2*sd[i]:null);
  const vma={}; VOL_MAS.forEach(n=>vma[n]=SMA(vol,n));
  return { ma, boll:{u:bu,m:mid,l:bl}, vma, macd:MACD(close), rsi:RSI(close,14), rsi6:RSI(close,6), kd:KD(bars,9,3,3) };
}
async function fetchStock(sid){
  if(HISTORY[sid]) return HISTORY[sid];
  try{
    const res=await fetch(`data/${sid}.json?v=${BUILD_V}`,{cache:"default"});
    if(!res.ok) return null;
    const j=await res.json();
    const n=j.d.length, bars=new Array(n);
    for(let i=0;i<n;i++) bars[i]=[j.d[i],j.o[i],j.h[i],j.l[i],j.c[i],j.v[i]];
    const o={name:j.n||"", market:j.m||"", ind:j.ind||"", bars, ts:(j.ts!=null?j.ts:n), t:(j.t||[]),
      mfs:(j.mfs!=null?j.mfs:n), mf:(j.mf||[]), fs:(j.fs!=null?j.fs:n), f:(j.f||[]), b4:(j.b4||[]),
      iss:(j.iss!=null?j.iss:null)};
    HISTORY[sid]=o; return o;
  }catch(e){ return null; }
}
/* 表頭『發行 / 流通張數』：流通 = 發行 ×(1−最新400張大戶%)。400張大戶已含董監＋法人大股東。 */
function fmtLots(n){ if(n==null||!isFinite(n)) return "—"; n=Math.round(n); const s=String(Math.abs(n)); let out=""; for(let i=0;i<s.length;i++){ if(i>0&&(s.length-i)%3===0) out+=","; out+=s[i]; } return (n<0?"-":"")+out; }
function renderFloat(o){
  const el=document.getElementById("cvFloat"); if(!el) return;
  const iss=(o&&o.iss!=null&&o.iss>0)?o.iss:null;
  if(iss==null){ el.innerHTML=""; el.style.display="none"; return; }
  const b4=(o&&o.b4)||[], big=b4.length?b4[b4.length-1][1]:null;   // 最新一週 400張大戶%
  let h=`<span class="cvfk">發行</span><span class="cvfv">${fmtLots(iss)}張</span>`;
  if(big!=null&&isFinite(big)){
    h+=`<span class="cvfsep">·</span><span class="cvfk">流通</span><span class="cvfv">${fmtLots(iss*(1-big/100))}張</span>`
      +`<span class="cvfnote">(扣董監大戶${big.toFixed(1)}%)</span>`;
  }
  el.innerHTML=h; el.style.display="";
}
async function openChart(sid){
  const o=await fetchStock(sid);
  if(!o||!o.bars||!o.bars.length){ alert("讀取「"+sid+"」資料失敗，請稍後再試。"); return; }
  const r=RESULTS.find(x=>x["代號"]===sid)||{};
  const td=(TRUST&&TRUST.data&&TRUST.data[sid])||null;
  const name=r["名稱"]||o.name||(td?td.name:"")||"";
  CH.sid=sid; CH.offset=0; CH.hover=null; CH.hoverY=null; CH.lock=null; CH.lockY=null;
  CH.volMode="vol"; CH.ts=o.ts; CH.t=o.t; CH.macdMode="macd"; CH.mfs=o.mfs; CH.mf=o.mf;
  CH.fs=o.fs; CH.f=o.f; CH.b4=o.b4||[]; CH.iss=(o.iss!=null?o.iss:null);
  document.getElementById("cvCode").textContent=sid; document.getElementById("cvName").textContent=name;
  const cind=document.getElementById("cvInd"), indv=o.ind||indLabel(sid); if(cind){ cind.textContent=indv||""; cind.style.display=indv?"":"none"; }
  const ccp=document.getElementById("cvConcepts"); if(ccp){ ccp.innerHTML=conceptChips(sid); }
  renderFloat(o);
  if(r["收盤"]!=null && r["漲跌%"]!=null){ const chg=num(r["漲跌%"]), cc=chg>0?UP:chg<0?DOWN:"var(--muted)";
    document.getElementById("cvChg").innerHTML=`<span style="color:${cc}">${r["收盤"]} (${chg>0?"+":""}${r["漲跌%"]}%)</span>`;
  } else { const lc=o.bars[o.bars.length-1][4];
    document.getElementById("cvChg").innerHTML=`<span style="color:var(--muted)">${lc!=null?lc.toFixed(2):""}</span>`; }
  const vb=document.getElementById("volModeBtn"); if(vb) vb.textContent="副圖:量";
  const mb=document.getElementById("macdModeBtn"); if(mb) mb.textContent="下圖:MACD";
  document.querySelectorAll(".pbtn[data-p]").forEach(b=>b.classList.toggle("on",b.dataset.p==="D"));
  setPeriod("D");
  const cv=document.getElementById("cv");
  if(!cv.classList.contains("open")){
    // 站內開圖：推一個歷史狀態，讓「返回」只關閉圖層、回到原本畫面（不離開頁面）。
    // 深連結(從處置頁等以 ?stk= 進來)時不推，讓返回直接回到來源頁面。
    if(!CHART_DEEPLINK){ try{ history.pushState({chart:sid}, "", "?stk="+encodeURIComponent(sid)); CHART_PUSHED=true; }catch(e){} }
  }
  cv.classList.add("open");
  // 圖層剛顯示時版面可能尚未定位(寬高為0)，下一影格用正確尺寸重畫一次，避免空白圖。
  requestAnimationFrame(()=>{ try{ if(CH.bars&&CH.bars.length) drawChart(); }catch(e){} });
}
let CHART_DEEPLINK=false, CHART_PUSHED=false;
function closeChart(){
  const cv=document.getElementById("cv");
  if(CHART_DEEPLINK){   // 從外部頁面(處置頁/通知等)進來：返回該來源頁面
    CHART_DEEPLINK=false;
    if(document.referrer && history.length>1){ history.back(); return; }
    cv.classList.remove("open"); try{ history.replaceState({}, "", location.pathname); }catch(e){}
    return;
  }
  if(CHART_PUSHED){ CHART_PUSHED=false; history.back(); return; }  // 觸發 popstate→關閉圖層
  cv.classList.remove("open");
}
window.addEventListener("popstate",()=>{ const cv=document.getElementById("cv"); if(cv&&cv.classList.contains("open")){ cv.classList.remove("open"); CHART_PUSHED=false; } });
function periodKey(ds,p){ if(p==="M") return ds.slice(0,7); if(p==="W"){ const a=ds.split("-").map(Number); const dt=new Date(Date.UTC(a[0],a[1]-1,a[2])); const day=(dt.getUTCDay()+6)%7; dt.setUTCDate(dt.getUTCDate()-day); return dt.toISOString().slice(0,10);} return ds; }
// 把「逐日買賣超(張)」依期別彙總成每根K棒的『淨買超(net)＋累計庫存(cum)』。投信/外資/主力共用。
function periodNetCum(daily, startIdx, arr, p, bars){
  const net={}, has={};
  for(let i=0;i<daily.length;i++){ const k=periodKey(daily[i][0],p); const val=(i>=startIdx)?arr[i-startIdx]:null;
    if(val!=null){ net[k]=(net[k]||0)+val; has[k]=true; } }
  const outNet=[], outCum=[]; let cum=0, started=false;
  for(const b of bars){ const k=periodKey(b.d,p); const hv=has[k]===true; const nv=hv?net[k]:null;
    outNet.push(nv); if(hv){ started=true; cum+=nv; } outCum.push(started?cum:null); }
  return {net:outNet, cum:outCum};
}
function setPeriod(p){
  CH.period=p; CH.offset=0; CH.hover=null;
  const o=HISTORY[CH.sid]; const daily=o.bars;
  CH.bars=aggregate(daily,p); CH.ind=computeInd(CH.bars);
  const tr=periodNetCum(daily, CH.ts, CH.t, p, CH.bars); CH.tnet=tr.net; CH.tcum=tr.cum;
  const fr=periodNetCum(daily, CH.fs, CH.f, p, CH.bars); CH.fnet=fr.net; CH.fcum=fr.cum;
  const mr=periodNetCum(daily, CH.mfs, CH.mf, p, CH.bars); CH.mnet=mr.net; CH.mcum=mr.cum;
  // 400張大戶持股%（週資料稀疏）：對每根K棒用「日期 ≤ 該棒」的最近一筆前向填補。
  CH.big=[]; { const b4=CH.b4||[]; let j=0, last=null;
    for(const bar of CH.bars){ while(j<b4.length && b4[j][0]<=bar.d){ last=b4[j][1]; j++; } CH.big.push(last); } }
  CH.count=Math.min(CH.bars.length, p==="D"?90:(p==="W"?80:60)); drawChart();
}
function toggleVol(){ if(!CH.bars.length)return; const i=VOL_MODES.indexOf(CH.volMode); CH.volMode=VOL_MODES[(i+1)%VOL_MODES.length]; const vb=document.getElementById("volModeBtn"); if(vb) vb.textContent=VOL_LABEL[CH.volMode]||"副圖:量"; drawChart(); }
function toggleMacd(){ if(!CH.bars.length)return; const i=MACD_MODES.indexOf(CH.macdMode); CH.macdMode=MACD_MODES[(i+1)%MACD_MODES.length]; const mb=document.getElementById("macdModeBtn"); if(mb) mb.textContent=MACD_LABEL[CH.macdMode]||"下圖:MACD"; drawChart(); }
document.querySelectorAll(".pbtn[data-p]").forEach(b=>b.addEventListener("click",()=>{document.querySelectorAll(".pbtn[data-p]").forEach(x=>x.classList.remove("on")); b.classList.add("on"); setPeriod(b.dataset.p);}));
function visRange(){ const N=CH.bars.length, cnt=Math.min(CH.count,N); let end=N-CH.offset; if(end>N)end=N; let start=end-cnt; if(start<0)start=0; return {start,end}; }
const PADL=50, PADR=12, GAP=8, DATEH=20;
function layout(W,H){ const usable=H-DATEH, ph=Math.round(usable*0.56), vh=Math.round(usable*0.20);
  return { price:{y0:0,y1:ph}, vol:{y0:ph+GAP,y1:ph+GAP+vh}, macd:{y0:ph+GAP+vh+GAP,y1:usable}, dateY:usable }; }
// 十字線輔助：水平虛線 + 左軸數值標籤
function hLine(ctx,W,y){ ctx.save(); ctx.strokeStyle="rgba(255,255,255,0.26)"; ctx.setLineDash([4,3]); ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(PADL,y); ctx.lineTo(W-PADR,y); ctx.stroke(); ctx.restore(); }
function axisTag(ctx,txt,y,col){ ctx.save(); ctx.font="10px sans-serif"; const w=ctx.measureText(txt).width+8, x0=Math.max(0,PADL-w-1); ctx.fillStyle=col||"#f5a524"; ctx.fillRect(x0,y-8,w,16); ctx.fillStyle="#000"; ctx.textAlign="center"; ctx.textBaseline="middle"; ctx.fillText(txt,x0+w/2,y); ctx.restore(); }
// 主力/投信/外資買賣超共用：淨買超柱 + 累計庫存線；回傳選取棒的十字線資訊(y/txt/col)。
function drawFlow(ctx,zone,start,end,xOf,bw,W,idx,netArr,cumArr,title,emptyMsg,cumCol){
  const y0=zone.y0, y1=zone.y1, hh=(y1-y0-4)/2, mid=Math.round((y0+y1)/2);
  ctx.strokeStyle="rgba(255,255,255,0.1)"; ctx.beginPath(); ctx.moveTo(PADL,mid); ctx.lineTo(W-PADR,mid); ctx.stroke();
  let any=false, nmax=1e-9; for(let i=start;i<end;i++){ const v=netArr[i]; if(v!=null){any=true; nmax=Math.max(nmax,Math.abs(v));} }
  if(!any){ ctx.fillStyle="#5e6f86"; ctx.textAlign="center"; ctx.textBaseline="middle"; ctx.fillText(emptyMsg,(PADL+W-PADR)/2,mid);
    ctx.textAlign="left"; ctx.textBaseline="top"; ctx.fillText(title,PADL+2,y0+2); return null; }
  const nbY=(v)=> mid - v/nmax*hh;
  ctx.fillStyle="#5e6f86"; ctx.textAlign="right"; ctx.textBaseline="middle"; ctx.fillText("±"+Math.round(nmax)+"張",PADL-6,y0+8);
  for(let i=start;i<end;i++){ const v=netArr[i]; if(v==null||v===0)continue; const x=xOf(i), y=nbY(v); ctx.fillStyle=v>=0?"rgba(255,77,79,.85)":"rgba(34,197,94,.85)"; const bodyW=Math.max(bw*0.6,1); ctx.fillRect(x-bodyW/2,Math.min(y,mid),bodyW,Math.abs(y-mid)||1); }
  let cmin=Infinity,cmax=-Infinity; for(let i=start;i<end;i++){ const v=cumArr[i]; if(v!=null){cmin=Math.min(cmin,v);cmax=Math.max(cmax,v);} }
  if(cmin<cmax){ const cY=(v)=> y1-2 - (v-cmin)/(cmax-cmin)*(y1-y0-4); ctx.strokeStyle=cumCol; ctx.lineWidth=1.6; ctx.beginPath(); let st=false; for(let i=start;i<end;i++){ const v=cumArr[i]; if(v==null){st=false;continue;} const x=xOf(i),y=cY(v); if(!st){ctx.moveTo(x,y);st=true;} else ctx.lineTo(x,y);} ctx.stroke(); }
  ctx.fillStyle=cumCol; ctx.textAlign="left"; ctx.textBaseline="top"; ctx.fillText(title,PADL+2,y0+2);
  const nv=netArr[idx]; return (nv!=null&&idx>=start&&idx<end)?{y:nbY(nv),txt:(nv>=0?"+":"")+Math.round(nv),col:nv>=0?"#fb3b41":"#1ec77a"}:null;
}
function drawMForce(ctx,L,start,end,xOf,bw,W,idx){
  return drawFlow(ctx,L.macd,start,end,xOf,bw,W,idx,CH.mnet,CH.mcum,"主力買賣超(柱) ▏累計(橘線)","此區間無主力買賣超資料（三大法人）","#f5a524");
}
function drawVolume(ctx,L,start,end,xOf,bw,W,idx){
  let vmax=0; for(let i=start;i<end;i++){ vmax=Math.max(vmax,CH.bars[i].v); VOL_MAS.forEach(m=>{const v=CH.ind.vma[m][i]; if(v!=null)vmax=Math.max(vmax,v);}); }
  vmax=vmax||1; const vY=(v)=> L.vol.y1 - v/vmax*(L.vol.y1-L.vol.y0-4);
  ctx.fillStyle="#5e6f86"; ctx.textAlign="right"; ctx.textBaseline="middle"; ctx.fillText(Math.round(vmax)+"張",PADL-6,L.vol.y0+8);
  for(let i=start;i<end;i++){ const b=CH.bars[i], x=xOf(i), up=b.c>=b.o; ctx.fillStyle=up?"rgba(255,77,79,.75)":"rgba(34,197,94,.75)"; const bodyW=Math.max(bw*0.6,1), y=vY(b.v); ctx.fillRect(x-bodyW/2,y,bodyW,L.vol.y1-y); }
  ctx.lineWidth=1.3; VOL_MAS.forEach(m=>{ ctx.strokeStyle=MACOLOR[m]; ctx.beginPath(); let st=false; for(let i=start;i<end;i++){ const v=CH.ind.vma[m][i]; if(v==null){st=false;continue;} const x=xOf(i),y=vY(v); if(!st){ctx.moveTo(x,y);st=true;} else ctx.lineTo(x,y);} ctx.stroke(); });
  ctx.fillStyle="#5e6f86"; ctx.textAlign="left"; ctx.textBaseline="top"; ctx.fillText("成交量",PADL+2,L.vol.y0+2);
  const b=CH.bars[idx]; return (b&&idx>=start&&idx<end)?{y:vY(b.v),txt:Math.round(b.v)+"張",col:"#8aa0b6"}:null;
}
function drawBig400(ctx,L,start,end,xOf,bw,W,idx){
  const y0=L.vol.y0, y1=L.vol.y1;
  let any=false, vmin=Infinity, vmax=-Infinity;
  for(let i=start;i<end;i++){ const v=CH.big[i]; if(v!=null){any=true; vmin=Math.min(vmin,v); vmax=Math.max(vmax,v);} }
  if(!any){ const mid=Math.round((y0+y1)/2); ctx.fillStyle="#5e6f86"; ctx.textAlign="center"; ctx.textBaseline="middle"; ctx.fillText("此區間無集保大戶資料（週更新）",(PADL+W-PADR)/2,mid);
    ctx.textAlign="left"; ctx.textBaseline="top"; ctx.fillText("400張大戶持股%",PADL+2,y0+2); return null; }
  if(vmax-vmin<0.4){ vmin-=0.4; vmax+=0.4; } const pad=(vmax-vmin)*0.12||0.2; vmin-=pad; vmax+=pad;
  const gY=(v)=> y1-3 - (v-vmin)/(vmax-vmin)*(y1-y0-8);
  ctx.fillStyle="#5e6f86"; ctx.textAlign="right"; ctx.textBaseline="middle"; ctx.fillText(vmax.toFixed(1)+"%",PADL-6,y0+8); ctx.fillText(vmin.toFixed(1)+"%",PADL-6,y1-6);
  ctx.strokeStyle="#c084fc"; ctx.lineWidth=1.7; ctx.beginPath(); let st=false;
  for(let i=start;i<end;i++){ const v=CH.big[i]; if(v==null){st=false;continue;} const x=xOf(i),y=gY(v); if(!st){ctx.moveTo(x,y);st=true;} else ctx.lineTo(x,y);} ctx.stroke();
  ctx.fillStyle="#c084fc"; ctx.textAlign="left"; ctx.textBaseline="top"; ctx.fillText("400張大戶持股%（週）",PADL+2,y0+2);
  const gv=CH.big[idx]; return (gv!=null&&idx>=start&&idx<end)?{y:gY(gv),txt:gv.toFixed(1)+"%",col:"#c084fc"}:null;
}
function drawMACD(ctx,L,start,end,xOf,bw,W,idx){
  const {dif,dea,osc}=CH.ind.macd; let mmax=1e-9; for(let i=start;i<end;i++){ [dif[i],dea[i],osc[i]].forEach(v=>{if(v!=null)mmax=Math.max(mmax,Math.abs(v));}); }
  const mMid=(L.macd.y0+L.macd.y1)/2, mH=(L.macd.y1-L.macd.y0-4)/2, mY=(v)=> mMid - v/mmax*mH;
  ctx.strokeStyle="rgba(255,255,255,0.12)"; ctx.beginPath(); ctx.moveTo(PADL,mMid); ctx.lineTo(W-PADR,mMid); ctx.stroke();
  ctx.fillStyle="#5e6f86"; ctx.textAlign="left"; ctx.textBaseline="top"; ctx.fillText("MACD(12,26,9)",PADL+2,L.macd.y0+2);
  for(let i=start;i<end;i++){ const v=osc[i]; if(v==null)continue; const x=xOf(i), y=mY(v); ctx.fillStyle=v>=0?"rgba(255,77,79,.8)":"rgba(34,197,94,.8)"; const bodyW=Math.max(bw*0.5,1); ctx.fillRect(x-bodyW/2,Math.min(y,mMid),bodyW,Math.abs(y-mMid)||1); }
  const dl=(arr,col)=>{ ctx.strokeStyle=col; ctx.lineWidth=1.3; ctx.beginPath(); let st=false; for(let i=start;i<end;i++){ const v=arr[i]; if(v==null){st=false;continue;} const x=xOf(i),y=mY(v); if(!st){ctx.moveTo(x,y);st=true;} else ctx.lineTo(x,y);} ctx.stroke(); };
  dl(dif,"#e8c34a"); dl(dea,"#4d9fff");
  const ov=osc[idx]; return (ov!=null&&idx>=start&&idx<end)?{y:mY(ov),txt:ov.toFixed(2),col:ov>=0?"#fb3b41":"#1ec77a"}:null;
}
// RSI / KD 共用：0~100 區間 + 參考線 + 雙線
function drawBand(ctx,L,start,end,xOf,idx,arrA,arrB,colA,colB,levels,title,valArr,valCol){
  const y0=L.macd.y0, y1=L.macd.y1, gY=(v)=> y1-2 - v/100*(y1-y0-4), xR=xOf(end-1)+20;
  levels.forEach(p=>{ ctx.strokeStyle=p[1]; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(PADL,gY(p[0])); ctx.lineTo(xR,gY(p[0])); ctx.stroke(); });
  ctx.fillStyle="#5e6f86"; ctx.textAlign="right"; ctx.textBaseline="middle"; ctx.fillText(""+levels[0][0],PADL-6,gY(levels[0][0])); ctx.fillText(""+levels[levels.length-1][0],PADL-6,gY(levels[levels.length-1][0]));
  const dl=(arr,col)=>{ ctx.strokeStyle=col; ctx.lineWidth=1.4; ctx.beginPath(); let st=false; for(let i=start;i<end;i++){ const v=arr[i]; if(v==null){st=false;continue;} const x=xOf(i),y=gY(v); if(!st){ctx.moveTo(x,y);st=true;} else ctx.lineTo(x,y);} ctx.stroke(); };
  dl(arrB,colB); dl(arrA,colA);
  ctx.fillStyle="#5e6f86"; ctx.textAlign="left"; ctx.textBaseline="top"; ctx.fillText(title,PADL+2,y0+2);
  const vv=valArr[idx]; return (vv!=null&&idx>=start&&idx<end)?{y:gY(vv),txt:vv.toFixed(1),col:valCol}:null;
}
function drawRSI(ctx,L,start,end,xOf,bw,W,idx){
  return drawBand(ctx,L,start,end,xOf,idx,CH.ind.rsi6,CH.ind.rsi,"#e8c34a","#4d9fff",
    [[70,"rgba(255,77,79,0.22)"],[50,"rgba(255,255,255,0.1)"],[30,"rgba(34,197,94,0.22)"]],"RSI 6(黃)/14(藍)",CH.ind.rsi,"#4d9fff");
}
function drawKD(ctx,L,start,end,xOf,bw,W,idx){
  return drawBand(ctx,L,start,end,xOf,idx,CH.ind.kd.k,CH.ind.kd.d,"#e8c34a","#4d9fff",
    [[80,"rgba(255,77,79,0.22)"],[50,"rgba(255,255,255,0.1)"],[20,"rgba(34,197,94,0.22)"]],"KD 9,3,3（K黃/D藍）",CH.ind.kd.k,"#e8c34a");
}
function drawChart(){
  const cv=document.getElementById("chartCanvas"), box=cv.parentElement, W=box.clientWidth, H=box.clientHeight, dpr=window.devicePixelRatio||1;
  cv.width=W*dpr; cv.height=H*dpr; const ctx=cv.getContext("2d"); ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,W,H);
  if(!CH.bars.length) return;
  const L=layout(W,H), {start,end}=visRange(), n=end-start, chartW=W-PADL-PADR, bw=chartW/n;
  const xOf=(i)=> PADL + (i-start+0.5)*bw, idx = CH.hover!=null ? CH.hover : end-1;
  let pmin=Infinity,pmax=-Infinity;
  for(let i=start;i<end;i++){ const b=CH.bars[i]; pmin=Math.min(pmin,b.l); pmax=Math.max(pmax,b.h);
    PRICE_MAS.forEach(m=>{const v=CH.ind.ma[m][i]; if(v!=null){pmin=Math.min(pmin,v);pmax=Math.max(pmax,v);}});
    const u=CH.ind.boll.u[i],l=CH.ind.boll.l[i]; if(u!=null)pmax=Math.max(pmax,u); if(l!=null)pmin=Math.min(pmin,l); }
  const pad=(pmax-pmin)*0.06||1; pmin-=pad; pmax+=pad;
  const pY=(v)=> L.price.y0+4 + (pmax-v)/(pmax-pmin)*(L.price.y1-L.price.y0-8);
  ctx.font="11px sans-serif"; ctx.textBaseline="middle";
  for(let g=0;g<=4;g++){ const v=pmin+(pmax-pmin)*g/4, y=pY(v); ctx.strokeStyle="rgba(255,255,255,0.05)"; ctx.beginPath(); ctx.moveTo(PADL,y); ctx.lineTo(W-PADR,y); ctx.stroke(); ctx.fillStyle="#5e6f86"; ctx.textAlign="right"; ctx.fillText(v.toFixed(2),PADL-6,y); }
  ctx.setLineDash([3,3]); ctx.lineWidth=1; ctx.strokeStyle=BOLL;
  [CH.ind.boll.u,CH.ind.boll.m,CH.ind.boll.l].forEach(arr=>{ ctx.beginPath(); let st=false; for(let i=start;i<end;i++){ const v=arr[i]; if(v==null){st=false;continue;} const x=xOf(i),y=pY(v); if(!st){ctx.moveTo(x,y);st=true;} else ctx.lineTo(x,y);} ctx.stroke(); });
  ctx.setLineDash([]);
  for(let i=start;i<end;i++){ const b=CH.bars[i], x=xOf(i), up=b.c>=b.o, col=up?UP:DOWN; ctx.strokeStyle=col; ctx.fillStyle=col; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(x,pY(b.h)); ctx.lineTo(x,pY(b.l)); ctx.stroke();
    const bodyW=Math.max(bw*0.6,1), yo=pY(b.o),yc=pY(b.c), top=Math.min(yo,yc), hgt=Math.max(Math.abs(yc-yo),1);
    ctx.fillRect(x-bodyW/2,top,bodyW,hgt); }   // 紅K/綠K 一律實體
  ctx.lineWidth=1.4;
  PRICE_MAS.forEach(m=>{ ctx.strokeStyle=MACOLOR[m]; ctx.beginPath(); let st=false; for(let i=start;i<end;i++){ const v=CH.ind.ma[m][i]; if(v==null){st=false;continue;} const x=xOf(i),y=pY(v); if(!st){ctx.moveTo(x,y);st=true;} else ctx.lineTo(x,y);} ctx.stroke(); });
  const N=CH.bars.length;
  PRICE_MAS.forEach(m=>{ const ki=(N-1)-(m-1); if(ki<start||ki>=end)return; const x=xOf(ki), y=L.price.y1-3; ctx.fillStyle=MACOLOR[m]; ctx.beginPath(); ctx.moveTo(x,y-7); ctx.lineTo(x-4,y); ctx.lineTo(x+4,y); ctx.closePath(); ctx.fill(); });
  // ===== 中間副圖：量 / 投信 / 外資 / 400張大戶（上方按鈕切換）=====
  let crossVol=null;
  if(CH.volMode==="inst"){ crossVol=drawFlow(ctx,L.vol,start,end,xOf,bw,W,idx,CH.tnet,CH.tcum,"投信買賣超 ▏庫存(黃線)","此區間無投信資料（投信約近一年）","#f5c518"); }
  else if(CH.volMode==="foreign"){ crossVol=drawFlow(ctx,L.vol,start,end,xOf,bw,W,idx,CH.fnet,CH.fcum,"外資買賣超 ▏庫存(黃線)","此區間無外資資料（外資約近一年）","#f5c518"); }
  else if(CH.volMode==="big400"){ crossVol=drawBig400(ctx,L,start,end,xOf,bw,W,idx); }
  else { crossVol=drawVolume(ctx,L,start,end,xOf,bw,W,idx); }
  // ===== 下圖：MACD / RSI / KD / 主力（上方按鈕切換）=====
  let crossMacd=null;
  if(CH.macdMode==="mforce"){ crossMacd=drawMForce(ctx,L,start,end,xOf,bw,W,idx); }
  else if(CH.macdMode==="rsi"){ crossMacd=drawRSI(ctx,L,start,end,xOf,bw,W,idx); }
  else if(CH.macdMode==="kd"){ crossMacd=drawKD(ctx,L,start,end,xOf,bw,W,idx); }
  else { crossMacd=drawMACD(ctx,L,start,end,xOf,bw,W,idx); }
  ctx.fillStyle="#5e6f86"; ctx.textAlign="center"; ctx.textBaseline="top";
  const ticks=Math.min(6,n); for(let t=0;t<ticks;t++){ const i=start+Math.floor((n-1)*t/(ticks-1||1)); ctx.fillText(CH.bars[i].d.slice(2),xOf(i),L.dateY+4); }
  // ===== 十字線（點擊鎖定 / 懸停）：垂直線貫穿三圖 + 各圖對應水平數值線 =====
  if(idx>=start&&idx<end){
    const x=xOf(idx);
    ctx.font="11px sans-serif";
    ctx.strokeStyle="rgba(255,255,255,0.34)"; ctx.setLineDash([4,3]); ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,L.dateY); ctx.stroke();
    ctx.setLineDash([]);
    // 主圖水平價格線：滑鼠/手指 Y 落在主圖內就讀該 Y 的價位，否則用選取棒收盤
    let hy, hp;
    if(CH.hoverY!=null && CH.hoverY>=L.price.y0 && CH.hoverY<=L.price.y1){
      hy=CH.hoverY; hp=pmax-(hy-(L.price.y0+4))/((L.price.y1-L.price.y0-8))*(pmax-pmin);
    } else { hp=CH.bars[idx].c; hy=pY(hp); }
    hLine(ctx,W,hy); axisTag(ctx,hp.toFixed(2),hy);
    // 副圖 / 下圖：對應選取棒數值的水平線 + 左軸標籤
    if(crossVol){ hLine(ctx,W,crossVol.y); axisTag(ctx,crossVol.txt,crossVol.y,crossVol.col); }
    if(crossMacd){ hLine(ctx,W,crossMacd.y); axisTag(ctx,crossMacd.txt,crossMacd.y,crossMacd.col); }
    // 底部日期標籤
    const dtxt=CH.bars[idx].d, dw=ctx.measureText(dtxt).width+10;
    const dx=Math.max(PADL, Math.min(W-PADR-dw, x-dw/2));
    ctx.fillStyle="#f5a524"; ctx.fillRect(dx, L.dateY+2, dw, 15);
    ctx.fillStyle="#000"; ctx.textAlign="center"; ctx.textBaseline="middle";
    ctx.fillText(dtxt, dx+dw/2, L.dateY+9);
  }
  updateReadout(idx);
}
function updateReadout(i){
  const b=CH.bars[i]; if(!b){document.getElementById("readout").innerHTML=""; return;}
  const prev=i>0?CH.bars[i-1].c:b.o, chg=((b.c-prev)/prev*100), cc=b.c>=prev?UP:DOWN;
  const it=(k,v,c,ex)=>`<div class="it${ex?" ext":""}"><span class="k">${k}</span><span class="v" ${c?`style="color:${c}"`:""}>${v}</span></div>`;
  const sgn=(x)=>(x>=0?"+":"")+Math.round(x);
  // 基本(收摺時也顯示)：開高低收漲跌量
  let h=it("日期",b.d)+it("開",b.o.toFixed(2),b.o>=prev?UP:DOWN)+it("高",b.h.toFixed(2),UP)+it("低",b.l.toFixed(2),DOWN)
       +it("收",b.c.toFixed(2),cc)+it("漲跌",(chg>=0?"+":"")+chg.toFixed(2)+"%",cc)+it("量",Math.round(b.v).toLocaleString()+" 張");
  // 進階(可收摺 .ext)：均線/布林/副圖/下圖數據
  PRICE_MAS.forEach(m=>{ const v=CH.ind.ma[m][i]; if(v!=null) h+=it("MA"+m, v.toFixed(2), MACOLOR[m], 1); });
  const bu=CH.ind.boll.u[i], bm=CH.ind.boll.m[i], bl=CH.ind.boll.l[i];
  if(bu!=null) h+=it("布林上", bu.toFixed(2), BOLL, 1);
  if(bm!=null) h+=it("布林中", bm.toFixed(2), BOLL, 1);
  if(bl!=null) h+=it("布林下", bl.toFixed(2), BOLL, 1);
  if(CH.volMode==="inst"){
    const tn=CH.tnet[i], tc=CH.tcum[i];
    if(tn!=null) h+=it("投信", sgn(tn)+" 張", tn>=0?UP:DOWN, 1);
    if(tc!=null) h+=it("投信庫存", Math.round(tc)+" 張", "#f5c518", 1);
  } else if(CH.volMode==="foreign"){
    const fn=CH.fnet[i], fc=CH.fcum[i];
    if(fn!=null) h+=it("外資", sgn(fn)+" 張", fn>=0?UP:DOWN, 1);
    if(fc!=null) h+=it("外資庫存", Math.round(fc)+" 張", "#f5c518", 1);
  } else if(CH.volMode==="big400"){
    const gv=CH.big[i];
    if(gv!=null) h+=it("400張大戶", gv.toFixed(2)+"%", "#c084fc", 1);
  }
  if(CH.macdMode==="mforce"){
    const mn=CH.mnet[i], mc=CH.mcum[i];
    if(mn!=null) h+=it("主力", sgn(mn)+" 張", mn>=0?UP:DOWN, 1);
    if(mc!=null) h+=it("主力累計", sgn(mc)+" 張", mc>=0?UP:DOWN, 1);
  } else if(CH.macdMode==="rsi"){
    const r6=CH.ind.rsi6[i], r14=CH.ind.rsi[i];
    if(r6!=null) h+=it("RSI6", r6.toFixed(1), "#e8c34a", 1);
    if(r14!=null) h+=it("RSI14", r14.toFixed(1), "#4d9fff", 1);
  } else if(CH.macdMode==="kd"){
    const kk=CH.ind.kd.k[i], dd=CH.ind.kd.d[i];
    if(kk!=null) h+=it("K", kk.toFixed(1), "#e8c34a", 1);
    if(dd!=null) h+=it("D", dd.toFixed(1), "#4d9fff", 1);
    if(kk!=null&&dd!=null) h+=it("K-D", (kk-dd).toFixed(1), (kk-dd)>=0?UP:DOWN, 1);
  } else if(CH.ind.macd){
    const dif=CH.ind.macd.dif[i], dea=CH.ind.macd.dea[i], osc=CH.ind.macd.osc[i];
    if(dif!=null) h+=it("DIF", dif.toFixed(2), "#e8c34a", 1);
    if(dea!=null) h+=it("DEA", dea.toFixed(2), "#4d9fff", 1);
    if(osc!=null) h+=it("OSC", osc.toFixed(2), osc>=0?UP:DOWN, 1);
  }
  document.getElementById("readout").innerHTML=h;
}
function toggleReadout(){
  const ro=document.getElementById("readout"), btn=document.getElementById("roToggle");
  const full=ro.classList.toggle("full");
  if(btn) btn.textContent=full?"▴ 收起指標數據":"▾ 展開指標數據(MA/布林/MACD…)";
}
const canvas=document.getElementById("chartCanvas");
function cx(clientX){ const r=canvas.getBoundingClientRect(); return clientX-r.left; }
// hover=即時顯示位置；lock=點擊/點按鎖定的位置（滑鼠移開後仍保留）。scrubAt(...,true) 會同時鎖定。
function scrubAt(x,y,lock){ const {start,end}=visRange(), n=end-start, bw=(canvas.parentElement.clientWidth-PADL-PADR)/n; let i=start+Math.floor((x-PADL)/bw); i=Math.max(start,Math.min(end-1,i)); CH.hover=i; CH.hoverY=(y==null?null:y); if(lock){ CH.lock=i; CH.lockY=CH.hoverY; } drawChart(); }
canvas.addEventListener("mousemove",e=>{ if(!CH.bars.length)return; scrubAt(e.offsetX, e.offsetY, false); });
canvas.addEventListener("mouseleave",()=>{ CH.hover=CH.lock; CH.hoverY=CH.lockY; drawChart(); });   // 移開→回到鎖定棒（未鎖定則回最新棒）
canvas.addEventListener("wheel",e=>{ if(!CH.bars.length)return; e.preventDefault(); const N=CH.bars.length, step=Math.max(2,Math.round(CH.count*0.12)); CH.count=Math.max(20,Math.min(N, CH.count+(e.deltaY>0?step:-step))); if(CH.offset>N-CH.count)CH.offset=Math.max(0,N-CH.count); CH.hover=null; CH.lock=null; CH.lockY=null; drawChart(); },{passive:false});
let drag=null, suppressClick=false;
canvas.addEventListener("mousedown",e=>{ drag={x:e.clientX,off:CH.offset}; });
window.addEventListener("mouseup",()=>{ drag=null; });
window.addEventListener("mousemove",e=>{ if(!drag||!CH.bars.length)return; if(Math.abs(e.clientX-drag.x)>4)suppressClick=true; const bw=(canvas.parentElement.clientWidth-PADL-PADR)/Math.min(CH.count,CH.bars.length), dB=Math.round((e.clientX-drag.x)/bw), N=CH.bars.length; CH.offset=Math.max(0,Math.min(N-Math.min(CH.count,N), drag.off+dB)); drawChart(); });
// 點擊＝在該日期鎖定十字線（副圖/下圖切換改用上方按鈕）
canvas.addEventListener("click",e=>{ if(suppressClick){suppressClick=false;return;} if(!CH.bars.length)return; scrubAt(e.offsetX, e.offsetY, true); });
let pinch=null, tap=null;
function tdist(t){ return Math.hypot(t[0].clientX-t[1].clientX, t[0].clientY-t[1].clientY); }
function tmid(t){ return cx((t[0].clientX+t[1].clientX)/2); }
canvas.addEventListener("touchstart",e=>{ if(!CH.bars.length)return;
  if(e.touches.length===1){ const r=canvas.getBoundingClientRect(); tap={x:e.touches[0].clientX, y:e.touches[0].clientY-r.top, t:Date.now(), moved:false}; scrubAt(cx(e.touches[0].clientX), e.touches[0].clientY-r.top, true); pinch=null; }
  else if(e.touches.length>=2){ tap=null; pinch={d:tdist(e.touches),off:CH.offset,cnt:CH.count,mid:tmid(e.touches)}; CH.hover=null; CH.hoverY=null; } },{passive:false});
canvas.addEventListener("touchmove",e=>{ if(!CH.bars.length)return; e.preventDefault();
  if(e.touches.length===1&&!pinch){ const r=canvas.getBoundingClientRect(); if(tap&&Math.abs(e.touches[0].clientX-tap.x)>8) tap.moved=true; scrubAt(cx(e.touches[0].clientX), e.touches[0].clientY-r.top, true); }
  else if(e.touches.length>=2&&pinch){ const nd=tdist(e.touches), ratio=pinch.d/(nd||1), N=CH.bars.length;
    CH.count=Math.max(20,Math.min(N,Math.round(pinch.cnt*ratio)));
    const bw=(canvas.parentElement.clientWidth-PADL-PADR)/Math.min(CH.count,N), dB=Math.round((tmid(e.touches)-pinch.mid)/bw);
    CH.offset=Math.max(0,Math.min(N-Math.min(CH.count,N), pinch.off+dB)); drawChart(); } },{passive:false});
canvas.addEventListener("touchend",e=>{ if(e.touches.length===0){ pinch=null; tap=null; } },{passive:false});
window.addEventListener("resize",()=>{ if(document.getElementById("cv").classList.contains("open"))drawChart(); });
document.addEventListener("keydown",e=>{ if(e.key==="Escape")closeChart(); });

/* ⑥ 首頁搜尋任意股 → 抓逐檔資料 → 開 K 線 */
let STKIDX=null;
async function loadIndex(){ if(STKIDX)return STKIDX; try{ const r=await fetch(`data/_index.json?v=${BUILD_V}`,{cache:"default"}); if(r.ok) STKIDX=await r.json(); }catch(e){} return STKIDX||[]; }
function renderSug(q){
  const box=document.getElementById("sugbox"); if(!q){ box.innerHTML=""; box.classList.remove("on"); return; }
  const idx=STKIDX||[], ql=q.toLowerCase(), hit=[];
  for(const e of idx){ if(e[0].toLowerCase().includes(ql)||(e[1]||"").toLowerCase().includes(ql)){ hit.push(e); if(hit.length>=14)break; } }
  if(!hit.length){ box.innerHTML='<div class="sugitem dim">查無此股（資料更新後才會出現新上市股）</div>'; box.classList.add("on"); return; }
  box.innerHTML=hit.map(e=>`<div class="sugitem" onclick="pickStock('${e[0]}')"><span class="sc">${e[0]}</span><span class="sn">${e[1]||""}${indTag(e[0])}${conceptInline(e[0])}</span><span class="sm">${e[2]||""}</span></div>`).join("");
  box.classList.add("on");
}
function pickStock(sid){ const box=document.getElementById("sugbox"); box.innerHTML=""; box.classList.remove("on"); const q=document.getElementById("stkq"); if(q)q.value=""; openChart(sid); }
(function(){ const q=document.getElementById("stkq"); if(!q)return;
  q.addEventListener("input",e=>renderSug(e.target.value.trim()));
  q.addEventListener("focus",loadIndex);
  document.addEventListener("click",e=>{ if(!e.target.closest(".searchwrap")){ const b=document.getElementById("sugbox"); if(b){b.innerHTML="";b.classList.remove("on");} } });
})();
loadIndex();

renderDD();
renderVol();
renderExtras();
renderFlows();
renderTrust();
render();
(function(){ try{ const sp=new URLSearchParams(location.search); const sid=sp.get("stk"); if(sid){ CHART_DEEPLINK=true; openChart(sid.trim()); } }catch(e){} })();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
