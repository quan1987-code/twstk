# -*- coding: utf-8 -*-
r"""
台美股每日市場分析面板產生器（tw_market_analysis.py）
================================================================
在既有 pipeline 之後執行（需先跑 tw_volume_breakout_screener_v2.py 產生 twstock.db
與 output/extras_*.json），輸出『單一自包含 HTML』：site/market.html
（CSS/JS/資料全部內嵌，下載後直接雙擊也能離線開啟）。

頁面內容（以專業台美股交易員每日盤後複盤/隔日備戰的需求設計）：
  1) 今日總覽：台股加權指數＋量能＋市場廣度、美股四大指數＋費半、亞股、
     總經監測（VIX/美10年債/美元指數/台幣/油/金/BTC）、TSM ADR 溢價。
  2) 盤勢研判：規則式自動評語（趨勢結構、量價、廣度、法人態度、背離警示）。
  3) 資金流向：三大法人估算日序列（近20日堆疊圖）＋官方當日彙總、
     融資融券、外資台指期未平倉、當日族群資金淨流入/流出 TOP、
     美股 11 大類股 ETF 相對強弱與風險偏好比率（小型/大型、等權/市值、可選/必選…）。
  4) 台股族群雷達（核心）：以 tw_industry 概念族群聚合，分四象限——
     今日漲勢主軸 / 今日跌勢主軸 / 底部起漲 / 持續強勢，每族群附成分股
     （點代號直接開 index.html 的 K 線）。
  5) 美股族群雷達：以主題 ETF（半導體/軟體/生技/軍工/太陽能/鈾…）跑同一套四象限。
  6) 每日監控清單：盤前/盤中/盤後 交易員 Routine，帶即時數值、可勾選（localStorage 記憶）。
  7) 風險儀表：VIX、利率變動、台股廣度、融資動向、外資期貨部位、美股廣度 → 綜合燈號。

資料來源：
  台股：twstock.db（price/inst/stock/industry，由 FinMind Sponsor 方案每日更新，與其他分頁同源）、
        output/extras_*.json（官方三大法人 BFI82U、融資融券 MI_MARGN、期交所外資未平倉）。
  美股/總經/全球：yfinance 為主來源（實測於 GitHub Actions 覆蓋完整、可用），
        Stooq 免金鑰 CSV 作缺漏備援（供 yfinance 被限流時頂替）；加權指數同此雙來源。

只『讀』DB、只『寫』site/market.html；任何一段資料失敗都以「暫無資料」呈現，
不會讓整個 pipeline 掛掉。

用法：
  python tw_market_analysis.py            # 正常（需 twstock.db；美股/總經連 Stooq）
  python tw_market_analysis.py --demo     # 離線示範（合成假資料，驗證輸出/前端）
  python tw_market_analysis.py --no-us    # 跳過美股抓取（只出台股區塊）
"""
import os
import sys
import json
import glob
import math
import random
import sqlite3
import argparse
import datetime as dt

try:
    import tw_industry
except Exception:
    tw_industry = None

DB_PATH = "twstock.db"
OUT_DIR = "site"
OUT_NAME = "market.html"

LOOKBACK_DAYS = 130          # 台股回看交易日數（位階/均線/量能）
SPARK_N = 60                 # 迷你走勢圖點數
INST_BAR_DAYS = 20           # 三大法人日序列堆疊圖天數
GROUP_MIN_STOCKS = 3         # 族群最少成分股
GROUP_TOPN = 8               # 每象限最多族群數
MEMBER_TOPN = 8              # 每族群最多列成分股
FLOW_TOPN = 8                # 當日族群資金流 in/out 各取 N

TPE_TZ = dt.timezone(dt.timedelta(hours=8))

WEEK_ZH = "一二三四五六日"


# ============================================================
#  小工具
# ============================================================
def _r(x, n=2):
    """安全四捨五入；NaN/Inf/None → None（JSON 才不會炸）。"""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    r = round(f, n)
    return 0.0 if r == 0 else r     # 消除 -0.0


def _pct(now, base, n=2):
    if now is None or base is None or base == 0:
        return None
    return _r((now / base - 1.0) * 100.0, n)


def _last(xs):
    return xs[-1] if xs else None


def _ma(xs, n):
    xs = [x for x in xs if x is not None]
    return (sum(xs[-n:]) / n) if len(xs) >= n else None


def _ret(closes, n):
    """近 n 個交易日報酬%（closes 由舊到新）。"""
    if len(closes) <= n:
        return None
    return _pct(closes[-1], closes[-1 - n])


def _pos(closes, highs=None, lows=None, window=120):
    """位階：最近收盤在近 window 日高低區間的位置 0~100。
    真實資料含停牌/新上市/處置的 None 缺口，min/max 前務必先濾 None。"""
    cs = [x for x in closes[-window:] if x is not None]
    if len(cs) < 20:
        return None
    hs = [x for x in (highs[-window:] if highs else []) if x is not None]
    ls = [x for x in (lows[-window:] if lows else []) if x is not None]
    hi = max(hs) if hs else max(cs)
    lo = min(ls) if ls else min(cs)
    if hi <= lo:
        return None
    return _r((cs[-1] - lo) / (hi - lo) * 100.0, 0)


def _spark(closes, n=SPARK_N):
    xs = [x for x in closes if x is not None][-n:]
    return [_r(x, 4) for x in xs]


def _zh_date(iso):
    """2026-07-03 → 07/03(五)"""
    if not iso:
        return "—"
    try:
        d = dt.date.fromisoformat(str(iso)[:10])
        return f"{d.month:02d}/{d.day:02d}({WEEK_ZH[d.weekday()]})"
    except ValueError:
        return str(iso)


# ============================================================
#  台股：讀 twstock.db → 市場統計 / 法人序列 / 族群指標
# ============================================================
def tw_dates(con, limit=LOOKBACK_DAYS):
    rows = con.execute(
        "SELECT DISTINCT date FROM price ORDER BY date DESC LIMIT ?", (limit,)).fetchall()
    return [r[0] for r in rows][::-1]     # 由舊到新


def _is_common_stock(sid):
    """近似判斷普通股：4 碼且非 00 開頭（排除 ETF/受益憑證），廣度統計用。"""
    s = str(sid)
    return len(s) == 4 and not s.startswith("00")


def tw_load(con):
    """回傳台股區所有原始統計；資料不足的欄位為 None。"""
    dates = tw_dates(con)
    if not dates:
        return None
    d0, dlast = dates[0], dates[-1]
    dprev = dates[-2] if len(dates) >= 2 else None

    # ---- 全市場 price 明細（近 LOOKBACK_DAYS）----
    rows = con.execute(
        "SELECT stock_id, date, close, high, low, amount FROM price WHERE date>=?", (d0,)).fetchall()
    series = {}                     # sid -> {date: (close, high, low, amount)}
    for sid, d, c, h, l, a in rows:
        if c is None:
            continue
        series.setdefault(sid, {})[d] = (c, h, l, a or 0.0)

    name_map = dict(con.execute("SELECT stock_id, name FROM stock"))
    mkt_map = dict(con.execute("SELECT stock_id, market FROM stock"))
    label_map = tw_industry.label_map(con) if tw_industry else {}

    # ---- 逐檔整理成對齊日期的序列 ----
    stk = {}                        # sid -> dict(closes, highs, lows, amts)
    for sid, m in series.items():
        cs, hs, ls, am = [], [], [], []
        for d in dates:
            v = m.get(d)
            cs.append(v[0] if v else None)
            hs.append(v[1] if v else None)
            ls.append(v[2] if v else None)
            am.append(v[3] if v else 0.0)
        stk[sid] = {"closes": cs, "highs": hs, "lows": ls, "amts": am}

    # ---- 市場成交值序列（億）----
    amt_daily = []
    for i, d in enumerate(dates):
        amt_daily.append(sum(s["amts"][i] for s in stk.values()) / 1e8)
    amt_today = _r(amt_daily[-1], 0)
    amt_avg20 = _r(_ma(amt_daily, 20), 0)
    mkt_amt_split = {}
    for sid, s in stk.items():
        mk = mkt_map.get(sid) or "其他"
        mkt_amt_split[mk] = mkt_amt_split.get(mk, 0.0) + s["amts"][-1] / 1e8

    # ---- 廣度統計（僅普通股）----
    up = down = flat = lu = ld = 0
    ab20 = ab60 = base20 = base60 = 0
    nh60 = nl60 = 0
    adv_dec_hist = []               # 近 20 日 (漲家-跌家) 供研判
    for i in range(max(0, len(dates) - 21), len(dates)):
        u = d_ = 0
        for sid, s in stk.items():
            if not _is_common_stock(sid):
                continue
            c, p = s["closes"][i], (s["closes"][i - 1] if i >= 1 else None)
            if c is None or p is None or p == 0:
                continue
            if c > p:
                u += 1
            elif c < p:
                d_ += 1
        adv_dec_hist.append(u - d_)
    for sid, s in stk.items():
        if not _is_common_stock(sid):
            continue
        cs = s["closes"]
        c, p = cs[-1], (cs[-2] if len(cs) >= 2 else None)
        if c is None or p is None or p == 0:
            continue
        chg = (c / p - 1) * 100
        if chg > 0.0001:
            up += 1
        elif chg < -0.0001:
            down += 1
        else:
            flat += 1
        if chg >= 9.8:
            lu += 1
        if chg <= -9.8:
            ld += 1
        m20, m60 = _ma(cs, 20), _ma(cs, 60)
        if m20 is not None:
            base20 += 1
            if c >= m20:
                ab20 += 1
        if m60 is not None:
            base60 += 1
            if c >= m60:
                ab60 += 1
        win = [x for x in cs[-60:] if x is not None]
        if len(win) >= 40:
            if c >= max(win):
                nh60 += 1
            if c <= min(win):
                nl60 += 1

    breadth = {
        "up": up, "down": down, "flat": flat,
        "limit_up": lu, "limit_down": ld,
        "pct_ab20": _r(ab20 / base20 * 100, 0) if base20 else None,
        "pct_ab60": _r(ab60 / base60 * 100, 0) if base60 else None,
        "nh60": nh60, "nl60": nl60,
        "adv_dec": adv_dec_hist,
    }

    # ---- 三大法人估算日序列（張×收盤 → 億）----
    inst_days = []
    irows = con.execute(
        "SELECT i.date, SUM(i.foreign_lots*p.close), SUM(i.trust_lots*p.close), "
        "SUM(i.dealer_lots*p.close), SUM(i.total_lots*p.close) "
        "FROM inst i JOIN price p ON p.stock_id=i.stock_id AND p.date=i.date "
        "WHERE i.total_lots IS NOT NULL GROUP BY i.date ORDER BY i.date DESC LIMIT 40").fetchall()
    for d, f, t, de, tot in irows[::-1]:
        k = 1000.0 / 1e8      # 張→股→億
        inst_days.append({"date": d, "f": _r((f or 0) * k, 1), "t": _r((t or 0) * k, 1),
                          "d": _r((de or 0) * k, 1), "tot": _r((tot or 0) * k, 1)})

    def _inst_sum(n):
        seg = inst_days[-n:]
        if not seg:
            return None
        return {"f": _r(sum(x["f"] or 0 for x in seg), 1),
                "t": _r(sum(x["t"] or 0 for x in seg), 1),
                "d": _r(sum(x["d"] or 0 for x in seg), 1),
                "tot": _r(sum(x["tot"] or 0 for x in seg), 1)}

    # ---- 族群聚合 ----
    groups = {}                     # label -> [sid,...]
    for sid in stk:
        lb = label_map.get(sid)
        if lb:
            groups.setdefault(lb, []).append(sid)

    # 每檔法人近 5/20 日淨額（億），供族群加總
    inst_stk = {}                   # sid -> (net5, net20) 億
    if inst_days:
        d20 = [x["date"] for x in inst_days][-20:]
        d5 = set(d20[-5:])
        d20 = set(d20)
        q = con.execute(
            "SELECT i.stock_id, i.date, i.total_lots*p.close*1000.0/1e8 "
            "FROM inst i JOIN price p ON p.stock_id=i.stock_id AND p.date=i.date "
            "WHERE i.total_lots IS NOT NULL AND i.date>=?", (min(d20),))
        for sid, d, v in q:
            if v is None:
                continue
            a5, a20 = inst_stk.get(sid, (0.0, 0.0))
            inst_stk[sid] = (a5 + (v if d in d5 else 0.0), a20 + (v if d in d20 else 0.0))

    glist = []
    for lb, sids in groups.items():
        mem = []
        for sid in sids:
            s = stk[sid]
            c = s["closes"][-1]
            p = s["closes"][-2] if len(s["closes"]) >= 2 else None
            if c is None or p in (None, 0):
                continue
            n5, n20 = inst_stk.get(sid, (0.0, 0.0))
            mem.append({
                "sid": sid, "name": name_map.get(sid, sid),
                "close": _r(c, 2), "ret1": _pct(c, p),
                "ret5": _ret(s["closes"], 5), "ret20": _ret(s["closes"], 20),
                "ret60": _ret(s["closes"], 60),
                "pos": _pos(s["closes"], s["highs"], s["lows"]),
                "amt": s["amts"][-1] / 1e8,
                "amt5": (_ma(s["amts"], 5) or 0) / 1e8,
                "amt20": (_ma(s["amts"], 20) or 0) / 1e8,
                "inst5": _r(n5, 1), "inst20": _r(n20, 1),
                "ab20": (c >= (_ma(s["closes"], 20) or 1e18)),
            })
        if len(mem) < GROUP_MIN_STOCKS:
            continue
        w = sum(m["amt"] for m in mem) or 1e-9

        def wavg(key):
            vals = [(m[key], m["amt"]) for m in mem if m[key] is not None]
            if not vals:
                return None
            tw_ = sum(a for _, a in vals) or 1e-9
            return _r(sum(v * a for v, a in vals) / tw_, 2)

        g = {
            "name": lb, "n": len(mem),
            "amt": _r(w, 1),
            "amt5": _r(sum(m["amt5"] for m in mem), 1),
            "amt20": _r(sum(m["amt20"] for m in mem), 1),
            "ret1": wavg("ret1"), "ret5": wavg("ret5"),
            "ret20": wavg("ret20"), "ret60": wavg("ret60"),
            "pos": wavg("pos"),
            "inst5": _r(sum(m["inst5"] or 0 for m in mem), 1),
            "inst20": _r(sum(m["inst20"] or 0 for m in mem), 1),
            "ab20r": _r(sum(1 for m in mem if m["ab20"]) / len(mem) * 100, 0),
            "members": sorted(mem, key=lambda m: -m["amt"])[:MEMBER_TOPN],
        }
        g["share"] = _r(w / (amt_daily[-1] or 1e-9) * 100, 1)
        base_share = (g["amt20"] or 0) / (amt_avg20 or 1e-9)
        g["sharex"] = _r((g["share"] or 0) / (base_share * 100), 2) if base_share > 0 else None
        glist.append(g)

    return {
        "dates": dates, "date": dlast, "prev_date": dprev,
        "amt_daily": amt_daily, "amt_today": amt_today, "amt_avg20": amt_avg20,
        "amt_split": {k: _r(v, 0) for k, v in sorted(mkt_amt_split.items())},
        "breadth": breadth,
        "inst_days": inst_days,
        "inst_sum5": _inst_sum(5), "inst_sum20": _inst_sum(20),
        "groups": glist,
    }


def tw_quadrants(glist):
    """四象限族群分類。輸入 tw_load 的 groups。"""
    def strip_members(g):
        out = dict(g)
        out["members"] = [
            {"sid": m["sid"], "name": m["name"], "ret1": m["ret1"],
             "ret20": m["ret20"], "pos": m["pos"], "inst20": m["inst20"]}
            for m in g["members"]]
        return out

    big = [g for g in glist if (g["amt"] or 0) >= 10]          # 當日成交值 ≥10 億才算主軸
    lead = sorted([g for g in big if (g["ret1"] or 0) > 0.5],
                  key=lambda g: -(g["ret1"] or 0))[:GROUP_TOPN]
    lag = sorted([g for g in big if (g["ret1"] or 0) < -0.5],
                 key=lambda g: (g["ret1"] or 0))[:GROUP_TOPN]

    bottom = []
    for g in glist:
        if (g["amt"] or 0) < 3 or g["pos"] is None:
            continue
        volx = (g["amt5"] or 0) / (g["amt20"] or 1e-9)
        if (g["pos"] <= 35 and (g["ret5"] or 0) >= 1.0 and volx >= 1.05
                and ((g["inst5"] or 0) > 0 or (g["inst20"] or 0) > 0)):
            g2 = dict(g)
            g2["volx"] = _r(volx, 2)
            g2["score"] = _r((g["ret5"] or 0) + (35 - g["pos"]) / 5.0
                             + min(max((g["inst20"] or 0), 0) / 10.0, 3) + volx, 2)
            bottom.append(g2)
    bottom = sorted(bottom, key=lambda g: -(g["score"] or 0))[:GROUP_TOPN]

    strong = []
    for g in glist:
        if (g["amt"] or 0) < 5 or g["pos"] is None:
            continue
        if (g["pos"] >= 70 and (g["ret20"] or 0) >= 5 and (g["ret60"] or -1) > 0
                and (g["ret5"] or 0) > -3 and (g["ab20r"] or 0) >= 50):
            g2 = dict(g)
            g2["score"] = _r((g["ret20"] or 0) + (g["ret60"] or 0) / 2.0
                             + min(max((g["inst20"] or 0), 0) / 10.0, 5), 2)
            strong.append(g2)
    strong = sorted(strong, key=lambda g: -(g["score"] or 0))[:GROUP_TOPN]

    return {"lead": [strip_members(g) for g in lead],
            "lag": [strip_members(g) for g in lag],
            "bottom": [strip_members(g) for g in bottom],
            "strong": [strip_members(g) for g in strong]}


def tw_group_flow(glist):
    """族群法人資金淨流入/流出 TOP。取『近 5 日三大法人淨額』而非單日：
    單日易被一筆大單或法人調節扭曲，5 日窗能呈現真正的資金移動方向，
    也對 FinMind 法人資料晚一天到位的情況更穩健。UI 標題同步標註（近5日法人・億）。"""
    xs = [g for g in glist if g.get("inst5") is not None and (g["amt"] or 0) >= 3]
    inflow = sorted([g for g in xs if (g["inst5"] or 0) > 0],
                    key=lambda g: -(g["inst5"] or 0))[:FLOW_TOPN]
    outflow = sorted([g for g in xs if (g["inst5"] or 0) < 0],
                     key=lambda g: (g["inst5"] or 0))[:FLOW_TOPN]
    pick = lambda g: {"name": g["name"], "amt": g["inst5"], "ret1": g["ret1"],
                      "ret5": g["ret5"], "share": g["share"]}
    return {"in": [pick(g) for g in inflow], "out": [pick(g) for g in outflow]}


def load_extras():
    cands = sorted(glob.glob(os.path.join("output", "extras_*.json")))
    if not cands:
        return {}
    try:
        with open(cands[-1], encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


# ============================================================
#  美股 / 總經：yfinance 主來源（GH Actions 實測可用）＋ Stooq 備援
# ============================================================
US_INDICES = [("^GSPC", "S&P 500"), ("^IXIC", "那斯達克"), ("^DJI", "道瓊工業"),
              ("^SOX", "費城半導體"), ("^RUT", "羅素2000")]
ASIA_INDICES = [("^N225", "日經225"), ("^KS11", "韓國KOSPI")]
US_SECTORS = [("XLK", "科技"), ("XLC", "通訊服務"), ("XLY", "非必需消費"), ("XLP", "必需消費"),
              ("XLV", "醫療保健"), ("XLF", "金融"), ("XLI", "工業"), ("XLE", "能源"),
              ("XLB", "原物料"), ("XLRE", "房地產"), ("XLU", "公用事業")]
US_THEMES = [("SMH", "半導體"), ("IGV", "軟體"), ("XBI", "生技"), ("KRE", "區域銀行"),
             ("ITA", "國防軍工"), ("TAN", "太陽能"), ("URA", "鈾/核能"), ("XME", "金屬礦業"),
             ("GDX", "金礦"), ("OIH", "油服"), ("XHB", "營建建商"), ("IYT", "運輸"),
             ("JETS", "航空"), ("ARKK", "創新成長"), ("KWEB", "中概網路"), ("FXI", "中國大型股"),
             ("EEM", "新興市場")]
US_MEGA = ["NVDA", "MSFT", "AAPL", "GOOGL", "AMZN", "META", "AVGO", "TSLA", "AMD", "MU"]
US_MACRO = [("^VIX", "VIX 恐慌指數"), ("^TNX", "美債10年殖利率"), ("DX-Y.NYB", "美元指數"),
            ("TWD=X", "美元/台幣"), ("CL=F", "WTI 原油"), ("GC=F", "黃金"), ("BTC-USD", "比特幣")]
US_RATIO_SYMS = ["SPY", "IWM", "RSP", "HYG", "IEF", "XLY", "XLP", "SMH"]
US_EXTRA = ["TSM", "2330.TW", "^TWII", "^TWOII"]


# Yahoo 代號 → Stooq 代號。個股/ETF 統一加 .us；指數 ^、期貨 .f、殖利率 10usy.b。
# 不在此表者（如 ^TWOII 櫃買）Stooq 無穩定對應，交給 yfinance 備援。
STOOQ_MAP = {
    "^GSPC": "^spx", "^IXIC": "^ndq", "^DJI": "^dji", "^SOX": "^sox", "^RUT": "^rut",
    "^N225": "^nkx", "^KS11": "^kospi", "^TWII": "^twse",
    "^VIX": "^vix", "^TNX": "10usy.b", "DX-Y.NYB": "^dxy",
    "TWD=X": "usdtwd", "CL=F": "cl.f", "GC=F": "gc.f", "BTC-USD": "btcusd",
    "TSM": "tsm.us", "2330.TW": "2330.tw",
    "SPY": "spy.us", "IWM": "iwm.us", "RSP": "rsp.us", "HYG": "hyg.us", "IEF": "ief.us",
    "XLK": "xlk.us", "XLC": "xlc.us", "XLY": "xly.us", "XLP": "xlp.us", "XLV": "xlv.us",
    "XLF": "xlf.us", "XLI": "xli.us", "XLE": "xle.us", "XLB": "xlb.us", "XLRE": "xlre.us",
    "XLU": "xlu.us", "SMH": "smh.us", "IGV": "igv.us", "XBI": "xbi.us", "KRE": "kre.us",
    "ITA": "ita.us", "TAN": "tan.us", "URA": "ura.us", "XME": "xme.us", "GDX": "gdx.us",
    "OIH": "oih.us", "XHB": "xhb.us", "IYT": "iyt.us", "JETS": "jets.us", "ARKK": "arkk.us",
    "KWEB": "kweb.us", "FXI": "fxi.us", "EEM": "eem.us",
    "NVDA": "nvda.us", "MSFT": "msft.us", "AAPL": "aapl.us", "GOOGL": "googl.us",
    "AMZN": "amzn.us", "META": "meta.us", "AVGO": "avgo.us", "TSLA": "tsla.us",
    "AMD": "amd.us", "MU": "mu.us",
}
# Stooq 的 10 年債殖利率(10usy.b)本身即為百分比；yfinance 的 ^TNX 為殖利率×10。
# 為讓下游一致，抓進來後一律正規化成「百分比」。


def all_us_symbols():
    syms = []
    for lst in (US_INDICES, ASIA_INDICES, US_SECTORS, US_THEMES, US_MACRO):
        syms += [s for s, _ in lst]
    syms += US_MEGA + US_RATIO_SYMS + US_EXTRA
    return sorted(set(syms))


def _to_f(x):
    try:
        v = float(x)
        return None if (v != v or v == 0.0 and x in ("", "N/D")) else v
    except (TypeError, ValueError):
        return None


def _parse_stooq_csv(text):
    """Stooq 日線 CSV → {dates,closes,highs,lows}；空/限流/錯誤回 None。
    正常標頭：Date,Open,High,Low,Close,Volume。"""
    text = (text or "").strip()
    low = text.lower()
    if (not text or low.startswith("<") or "no data" in low
            or "exceeded" in low or not low.startswith("date")):
        return None
    dates, closes, highs, lows = [], [], [], []
    for ln in text.splitlines()[1:]:
        p = ln.split(",")
        if len(p) < 5:
            continue
        c = _to_f(p[4])
        if c is None:
            continue
        dates.append(p[0])
        closes.append(c)
        highs.append(_to_f(p[2]))
        lows.append(_to_f(p[3]))
    if len(closes) < 2:
        return None
    return {"dates": dates, "closes": closes, "highs": highs, "lows": lows}


def _fetch_stooq(symbols, start_yyyymmdd, session=None):
    """對每個 Yahoo 代號查 STOOQ_MAP，逐檔抓 Stooq 日線 CSV。回傳 {yahoo_sym: series}。"""
    import requests
    sess = session or requests.Session()
    out, ok, miss = {}, 0, []
    for ysym in symbols:
        ssym = STOOQ_MAP.get(ysym)
        if not ssym:
            miss.append(ysym)
            continue
        url = f"https://stooq.com/q/d/l/?s={ssym}&i=d&d1={start_yyyymmdd}"
        try:
            r = sess.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
            s = _parse_stooq_csv(r.text)
        except Exception as e:
            print(f"  [stooq] {ysym}({ssym}) 失敗：{e}")
            s = None
        if s:
            out[ysym] = s
            ok += 1
        else:
            miss.append(ysym)
    print(f"  [stooq] 成功 {ok} 檔；待備援 {len(miss)} 檔：{miss}")
    return out


def _fetch_yf(symbols):
    """yfinance 備援：批次抓日線（10 個月）。回傳 {sym: series}。"""
    import yfinance as yf
    import pandas as pd
    out = {}
    df = yf.download(symbols, period="10mo", interval="1d", group_by="ticker",
                     auto_adjust=True, threads=True, progress=False)
    if df is None or df.empty:
        return out
    multi = isinstance(df.columns, pd.MultiIndex)
    for sym in symbols:
        try:
            sub = (df[sym] if multi else df).dropna(subset=["Close"])
            if sub.empty:
                continue
            out[sym] = {
                "dates": [d.strftime("%Y-%m-%d") for d in sub.index],
                "closes": [float(x) for x in sub["Close"].tolist()],
                "highs": [None if x != x else float(x) for x in sub["High"].tolist()],
                "lows": [None if x != x else float(x) for x in sub["Low"].tolist()],
            }
        except Exception:
            continue
    return out


def _normalize_tnx(data):
    """把 ^TNX 統一成百分比。不同來源量級不一（Yahoo 歷來為殖利率×10＝約 44，
    近期部分回傳已是 4.4；Stooq 10usy.b 為 4.4）。以量級判定：整段序列最大值 >20
    視為 ×10 型態、統一除以 10；否則已是百分比、原樣保留。"""
    s = data.get("^TNX")
    if not s:
        return
    vals = [v for v in s["closes"] if v is not None]
    if vals and max(vals) > 20:
        for k in ("closes", "highs", "lows"):
            s[k] = [None if v is None else v / 10.0 for v in s[k]]


def fetch_us(symbols, start_yyyymmdd=None):
    """美股/總經日線。主來源 yfinance（實測於 GitHub Actions 可用、覆蓋完整），
    Stooq 作為缺漏備援（免金鑰、供 yfinance 被限流時頂替）。
    回傳 {yahoo_sym: {dates,closes,highs,lows}}。"""
    if start_yyyymmdd is None:
        start_yyyymmdd = "20240101"
    out = {}
    try:
        out = _fetch_yf(symbols)
        print(f"  [yfinance] 主來源取得 {len(out)} 檔")
    except Exception as e:
        print(f"yfinance 主來源失敗（改用 Stooq）：{e}")
    missing = [s for s in symbols if s not in out]
    if missing:
        try:
            sout = _fetch_stooq(missing, start_yyyymmdd)
            if sout:
                print(f"  [stooq] 備援補回 {len(sout)} 檔")
                out.update(sout)
        except Exception as e:
            print(f"Stooq 備援失敗：{e}")
        still = [s for s in symbols if s not in out]
        if still:
            print(f"  兩來源皆未取得：{still}")
    _normalize_tnx(out)
    return out


def _series_metrics(s):
    cs = s["closes"]
    return {
        "close": _r(cs[-1], 2), "chg_pct": _ret(cs, 1),
        "ret5": _ret(cs, 5), "ret20": _ret(cs, 20), "ret60": _ret(cs, 60),
        "pos": _pos(cs, s.get("highs"), s.get("lows")),
        "spark": _spark(cs), "date": s["dates"][-1] if s.get("dates") else None,
    }


def us_build(data):
    """把 yfinance 序列組成前端 payload 的 us 區塊。data 缺誰就跳過誰。"""
    if not data:
        return None

    def met(sym):
        return _series_metrics(data[sym]) if sym in data else None

    spy = met("SPY")
    indices = []
    for sym, name in US_INDICES:
        m = met(sym)
        if m:
            m.update({"sym": sym, "name": name})
            indices.append(m)
    asia = []
    for sym, name in ASIA_INDICES:
        m = met(sym)
        if m:
            m.update({"sym": sym, "name": name})
            asia.append(m)

    macro = []
    for sym, name in US_MACRO:
        m = met(sym)
        if not m:
            continue
        # ^TNX 於 fetch_us 已正規化為百分比，這裡直接用不再除 10
        val, unit, dec = m["close"], "", 2
        if sym == "^TNX":
            val, unit, dec = _r(m["close"], 2), "%", 2
        m5 = None
        cl = data[sym]["closes"]
        if sym in data and len(cl) > 5 and cl[-1] is not None and cl[-6] is not None:
            m5 = _r(cl[-1] - cl[-6], 2)
        macro.append({"sym": sym, "name": name, "val": val, "unit": unit,
                      "chg_pct": m["chg_pct"], "chg5_abs": m5, "spark": m["spark"]})

    # TSM ADR 溢價（1 ADR = 5 股 2330）
    adr = None
    if all(s in data for s in ("TSM", "2330.TW", "TWD=X")):
        tsm = data["TSM"]["closes"][-1]
        twd = data["TWD=X"]["closes"][-1]
        px = data["2330.TW"]["closes"][-1]
        if px and twd:
            adr = {"premium": _r((tsm * twd) / (px * 5.0) * 100 - 100, 1),
                   "tsm": _r(tsm, 2), "px2330": _r(px, 1), "twd": _r(twd, 3)}

    mega = []
    for sym in US_MEGA:
        m = met(sym)
        if m:
            mega.append({"sym": sym, "ret1": m["chg_pct"], "ret5": m["ret5"]})

    sectors = []
    for sym, name in US_SECTORS:
        m = met(sym)
        if not m:
            continue
        rs20 = (_r((m["ret20"] or 0) - (spy["ret20"] or 0), 1)
                if (spy and m["ret20"] is not None and spy["ret20"] is not None) else None)
        sectors.append({"sym": sym, "name": name, "ret1": m["chg_pct"], "ret5": m["ret5"],
                        "ret20": m["ret20"], "rs20": rs20, "pos": m["pos"]})

    def ratio(a, b, n):
        if a not in data or b not in data:
            return None
        ra, rb = _ret(data[a]["closes"], n), _ret(data[b]["closes"], n)
        if ra is None or rb is None:
            return None
        return _r(ra - rb, 1)

    ratios = []
    for a, b, name, hint in [
            ("IWM", "SPY", "小型股 − 大型股", "正值＝資金敢冒險（Risk-on）"),
            ("RSP", "SPY", "等權重 − 市值加權", "正值＝上漲擴散、非只靠權值撐"),
            ("XLY", "XLP", "非必需 − 必需消費", "正值＝消費信心/風險偏好升"),
            ("SMH", "SPY", "半導體 − 大盤", "台股連動最高的領先指標"),
            ("HYG", "IEF", "高收益債 − 公債", "正值＝信用市場無壓力")]:
        v5, v20 = ratio(a, b, 5), ratio(a, b, 20)
        if v5 is None and v20 is None:
            continue
        ratios.append({"name": name, "hint": hint, "v5": v5, "v20": v20})

    # 主題 ETF 四象限（與台股同邏輯，但用 RS 取代法人）
    themes = []
    for sym, name in US_SECTORS + US_THEMES:
        m = met(sym)
        if not m:
            continue
        rs20 = (_r((m["ret20"] or 0) - (spy["ret20"] or 0), 1)
                if (spy and m["ret20"] is not None and spy["ret20"] is not None) else None)
        rs5 = None
        if spy and m["ret5"] is not None and spy["ret5"] is not None:
            rs5 = _r(m["ret5"] - spy["ret5"], 1)
        themes.append({"sym": sym, "name": name, "ret1": m["chg_pct"], "ret5": m["ret5"],
                       "ret20": m["ret20"], "ret60": m["ret60"], "pos": m["pos"],
                       "rs5": rs5, "rs20": rs20})

    lead = sorted([t for t in themes if (t["ret1"] or 0) > 0.3],
                  key=lambda t: -(t["ret1"] or 0))[:GROUP_TOPN]
    lag = sorted([t for t in themes if (t["ret1"] or 0) < -0.3],
                 key=lambda t: (t["ret1"] or 0))[:GROUP_TOPN]
    bottom = sorted([t for t in themes
                     if t["pos"] is not None and t["pos"] <= 35
                     and (t["ret5"] or 0) > 0 and (t["rs5"] or 0) > 0],
                    key=lambda t: -((t["ret5"] or 0) + (35 - t["pos"]) / 5.0))[:GROUP_TOPN]
    strong = sorted([t for t in themes
                     if t["pos"] is not None and t["pos"] >= 70
                     and (t["rs20"] or 0) > 0 and (t["ret60"] or 0) > 0],
                    key=lambda t: -((t["ret20"] or 0) + (t["ret60"] or 0) / 2.0))[:GROUP_TOPN]

    return {"indices": indices, "asia": asia, "macro": macro, "adr": adr, "mega": mega,
            "sectors": sectors, "ratios": ratios,
            "quad": {"lead": lead, "lag": lag, "bottom": bottom, "strong": strong},
            "date": indices[0]["date"] if indices else None}


# ============================================================
#  規則式研判 / 風險儀表 / 監控清單
# ============================================================
def _sign_zh(v, pos="買超", neg="賣超"):
    if v is None:
        return "—"
    return f"{pos} {abs(v):,.0f} 億" if v >= 0 else f"{neg} {abs(v):,.0f} 億"


def _spct(v, dec=2):
    """帶正負號的百分比字串；接近 0 一律顯示 0（避免 -0.00%）。"""
    if v is None:
        return "—"
    if abs(v) < 0.5 * 10 ** (-dec):
        v = 0.0
    return f"{v:+.{dec}f}%"


def tw_comment(tw, twii, extras, quad):
    """台股規則式盤勢評語。回傳 (段落list, 風險list, 燈號)。"""
    paras, risks = [], []
    b = tw["breadth"]
    light = "neutral"

    # 指數結構
    idx_txt = ""
    trend_score = 0
    if twii and twii.get("close") is not None:
        c = twii["close"]
        m5, m20, m60 = twii.get("ma5"), twii.get("ma20"), twii.get("ma60")
        stat = []
        if m20 is not None:
            stat.append("站上月線" if c >= m20 else "跌破月線")
            trend_score += 1 if c >= m20 else -1
        if m60 is not None:
            stat.append("季線之上" if c >= m60 else "季線之下")
            trend_score += 1 if c >= m60 else -1
        chg = twii.get("chg_pct")
        idx_txt = (f"加權指數收 {c:,.0f} 點（{chg:+.2f}%），" if chg is not None
                   else f"加權指數收 {c:,.0f} 點，") + "、".join(stat) + "。"

    # 量價
    ratio = (tw["amt_today"] / tw["amt_avg20"]) if (tw["amt_today"] and tw["amt_avg20"]) else None
    if ratio is not None:
        vol_zh = ("顯著放量" if ratio >= 1.3 else "溫和放量" if ratio >= 1.1
                  else "量能持平" if ratio >= 0.9 else "明顯量縮")
        idx_txt += f"全市場成交 {tw['amt_today']:,.0f} 億，為 20 日均量的 {ratio:.2f} 倍（{vol_zh}）。"
    paras.append(idx_txt or "加權指數資料暫缺，以下以全市場統計研判。")

    # 廣度
    tot = (b["up"] or 0) + (b["down"] or 0)
    btxt = ""
    if tot:
        r = b["up"] / tot * 100
        tone = ("普漲格局，短多動能健康" if r >= 65 else "多方略佔優" if r >= 55
                else "多空拉鋸" if r >= 45 else "空方略佔優" if r >= 35 else "普跌格局，注意風險")
        btxt = (f"上漲 {b['up']} 家／下跌 {b['down']} 家（漲家比 {r:.0f}%），{tone}；"
                f"漲停 {b['limit_up']} 檔、跌停 {b['limit_down']} 檔。")
        trend_score += 1 if r >= 55 else (-1 if r <= 45 else 0)
    if b["pct_ab60"] is not None:
        btxt += f"全市場約 {b['pct_ab60']:.0f}% 個股站上季線"
        btxt += f"、{b['pct_ab20']:.0f}% 站上月線，" if b["pct_ab20"] is not None else "，"
        btxt += ("中期體質偏多。" if b["pct_ab60"] >= 55 else
                 "中期體質中性。" if b["pct_ab60"] >= 40 else "中期體質偏空，反彈以短線視之。")
    if btxt:
        paras.append(btxt)

    # 法人
    inst_txt = ""
    inst3 = (extras or {}).get("inst3")
    last = tw["inst_days"][-1] if tw["inst_days"] else None
    if inst3 and inst3.get("total") is not None:
        inst_txt = (f"三大法人合計{_sign_zh(inst3['total'])}"
                    f"（外資 {inst3.get('foreign') or 0:+,.0f}、投信 {inst3.get('trust') or 0:+,.0f}、"
                    f"自營 {inst3.get('dealer') or 0:+,.0f} 億）。")
        trend_score += 1 if inst3["total"] > 0 else -1
    elif last and last.get("tot") is not None:
        inst_txt = f"三大法人估計{_sign_zh(last['tot'])}（依個股張數×收盤估算）。"
        trend_score += 1 if last["tot"] > 0 else -1
    s20 = tw.get("inst_sum20")
    if s20 and s20.get("tot") is not None:
        inst_txt += f"近 20 日累計{_sign_zh(s20['tot'])}"
        if s20.get("t") is not None:
            inst_txt += f"，其中投信 {s20['t']:+,.0f} 億"
        inst_txt += "。"
    if inst_txt:
        paras.append(inst_txt)

    # 資金主流
    if quad["lead"]:
        names = "、".join(g["name"] for g in quad["lead"][:3])
        t = f"今日資金主軸：{names}"
        if quad["lag"]:
            t += f"；弱勢族群：{'、'.join(g['name'] for g in quad['lag'][:3])}"
        t += "。"
        if quad["bottom"]:
            t += f"底部轉強訊號出現在 {'、'.join(g['name'] for g in quad['bottom'][:3])}，可留意是否為新一輪輪動起點。"
        paras.append(t)

    # 風險提示
    if twii and twii.get("chg_pct") is not None and tot:
        r = b["up"] / tot * 100
        if twii["chg_pct"] > 0.3 and r < 45:
            risks.append("指數漲但下跌家數多（權值撐盤、廣度背離），追價宜保守。")
        if twii["chg_pct"] < -0.3 and r > 55:
            risks.append("指數跌但多數個股上漲（權值拖累），中小型股結構未轉壞。")
    if ratio is not None and twii and twii.get("chg_pct") is not None:
        if ratio >= 1.3 and twii["chg_pct"] < -0.8:
            risks.append("放量長黑：量增價跌屬轉弱訊號，留意籌碼鬆動。")
        if ratio <= 0.8 and twii["chg_pct"] > 0.5:
            risks.append("量縮上漲：追價力道不足，慎防假突破。")
    mg = (extras or {}).get("margin")
    if mg and mg.get("fin_chg") is not None and twii and twii.get("chg_pct") is not None:
        if mg["fin_chg"] > 0 and twii["chg_pct"] < -0.5:
            risks.append(f"大盤下跌但融資仍增 {mg['fin_chg']:+.1f} 億：散戶逆勢加碼，籌碼轉差。")
    txf = (extras or {}).get("txf_foreign")
    if txf and txf.get("net_oi") is not None and txf["net_oi"] < -30000:
        risks.append(f"外資台指期淨空單 {abs(txf['net_oi']):,.0f} 口（偏空避險部位大）。")

    light = "bull" if trend_score >= 2 else ("bear" if trend_score <= -2 else "neutral")
    return paras, risks, light


def us_comment(us):
    if not us:
        return ["美股資料暫時無法取得。"], [], "neutral"
    paras, risks = [], []
    score = 0
    idx = {i["sym"]: i for i in us["indices"]}
    seg = []
    for sym, zh in (("^GSPC", "標普"), ("^IXIC", "那指"), ("^DJI", "道瓊"),
                    ("^SOX", "費半"), ("^RUT", "羅素2000")):
        m = idx.get(sym)
        if m and m["chg_pct"] is not None:
            seg.append(f"{zh} {_spct(m['chg_pct'])}")
            if sym in ("^GSPC", "^IXIC"):
                score += 1 if m["chg_pct"] > 0 else -1
    if seg:
        paras.append("美股最近收盤：" + "、".join(seg) + "。")
    sox = idx.get("^SOX")
    if sox and sox["chg_pct"] is not None:
        if abs(sox["chg_pct"]) >= 1.5:
            t = (f"費半{'大漲' if sox['chg_pct'] > 0 else '大跌'} "
                 f"{abs(sox['chg_pct']):.1f}%，對台股電子股方向有直接指引。")
            if paras:
                paras[-1] += t
            else:
                paras.append(t)
        score += 1 if sox["chg_pct"] > 0 else -1

    ups = [s for s in us["sectors"] if (s["ret1"] or 0) > 0]
    if us["sectors"]:
        t = f"11 大類股中 {len(ups)} 個上漲"
        lead = sorted(us["sectors"], key=lambda s: -(s["ret1"] or 0))[:2]
        lagg = sorted(us["sectors"], key=lambda s: (s["ret1"] or 0))[:2]
        t += f"；最強：{'、'.join(s['name'] for s in lead)}，最弱：{'、'.join(s['name'] for s in lagg)}。"
        defensive = {"XLP", "XLU", "XLV", "XLRE"}
        if all(s["sym"] in defensive for s in lead):
            t += "資金移往防禦類股，屬 Risk-off 訊號。"
            score -= 1
        paras.append(t)
        score += 1 if len(ups) >= 7 else (-1 if len(ups) <= 4 else 0)

    mac = {m["sym"]: m for m in us["macro"]}
    vix = mac.get("^VIX")
    tnx = mac.get("^TNX")
    mtxt = []
    if vix and vix["val"] is not None:
        lvl = ("低檔（市場自滿區，反而留意突發波動）" if vix["val"] < 14 else
               "正常區間" if vix["val"] < 20 else
               "偏高（避險情緒升溫）" if vix["val"] < 28 else "恐慌區")
        mtxt.append(f"VIX {vix['val']:.1f} 屬{lvl}")
        if vix["val"] >= 22:
            score -= 1
            risks.append(f"VIX {vix['val']:.1f} 偏高：波動放大環境，部位宜降。")
    if tnx and tnx["val"] is not None:
        t = f"美 10 年期殖利率 {tnx['val']:.2f}%"
        if tnx.get("chg5_abs") is not None:
            t += f"（5 日 {tnx['chg5_abs']:+.2f} 個百分點）"
            if abs(tnx["chg5_abs"]) >= 0.20:
                risks.append("利率一週內波動超過 20bp：評價面擾動，成長股波動加大。")
                score -= 1
        mtxt.append(t)
    if mtxt:
        paras.append("、".join(mtxt) + "。")

    if us.get("adr") and us["adr"].get("premium") is not None:
        p = us["adr"]["premium"]
        t = f"TSM ADR 相對 2330 溢價 {p:+.1f}%"
        t += ("，美方對台積電評價明顯較高，台股電子開盤偏多氛圍。" if p >= 15 else
              "，屬常態區間。" if p >= 5 else "，溢價收斂，留意外資調節壓力。")
        paras.append(t)

    for r in us.get("ratios", []):
        if r["name"].startswith("高收益債") and r["v20"] is not None and r["v20"] < -1.5:
            risks.append("信用利差走闊（HYG 相對 IEF 轉弱）：風險資產的領先警訊。")

    light = "bull" if score >= 2 else ("bear" if score <= -2 else "neutral")
    return paras, risks, light


def build_gauges(tw, twii, us, extras):
    """風險儀表：green / yellow / red。"""
    gs = []

    def add(name, val_str, light, hint):
        gs.append({"name": name, "val": val_str, "light": light, "hint": hint})

    mac = {m["sym"]: m for m in (us or {}).get("macro", [])}
    vix = mac.get("^VIX")
    if vix and vix["val"] is not None:
        add("VIX 波動率", f"{vix['val']:.1f}",
            "green" if vix["val"] < 17 else ("yellow" if vix["val"] < 24 else "red"),
            "<17 平穩｜17–24 升溫｜>24 恐慌")
    tnx = mac.get("^TNX")
    if tnx and tnx.get("chg5_abs") is not None:
        c = abs(tnx["chg5_abs"])
        add("美債利率 5 日變動", f"{tnx['chg5_abs']:+.2f}%",
            "green" if c < 0.12 else ("yellow" if c < 0.25 else "red"),
            "利率急動對評價面衝擊大")
    b = (tw or {}).get("breadth") or {}
    if b.get("pct_ab60") is not None:
        v = b["pct_ab60"]
        add("台股廣度（站上季線）", f"{v:.0f}%",
            "green" if v >= 55 else ("yellow" if v >= 40 else "red"),
            "≥55% 體質佳｜<40% 弱勢市場")
    mg = (extras or {}).get("margin")
    if mg and mg.get("fin_chg") is not None:
        chg = mg["fin_chg"]
        idx_chg = (twii or {}).get("chg_pct")
        light = "green"
        if idx_chg is not None and chg > 0 and idx_chg < -0.5:
            light = "red"
        elif chg > 30:
            light = "yellow"
        add("融資動向", f"{chg:+,.1f} 億", light, "跌勢中融資逆增＝籌碼轉差")
    txf = (extras or {}).get("txf_foreign")
    if txf and txf.get("net_oi") is not None:
        v = txf["net_oi"]
        add("外資台指期淨部位", f"{v:+,.0f} 口",
            "green" if v > -10000 else ("yellow" if v > -30000 else "red"),
            "大量淨空單＝外資避險/看空")
    if us and us.get("sectors"):
        ups = sum(1 for s in us["sectors"] if (s["ret1"] or 0) > 0)
        add("美股類股廣度", f"{ups}/11 上漲",
            "green" if ups >= 7 else ("yellow" if ups >= 4 else "red"),
            "上漲類股家數（最近收盤）")

    reds = sum(1 for g in gs if g["light"] == "red")
    yels = sum(1 for g in gs if g["light"] == "yellow")
    overall = "red" if reds >= 2 else ("yellow" if (reds == 1 or yels >= 3) else "green")
    zh = {"green": "整體風險環境：綠燈（可正常執行策略，紀律停損照舊）",
          "yellow": "整體風險環境：黃燈（降低槓桿、控制單一部位）",
          "red": "整體風險環境：紅燈（防禦優先，現金為部位的一種）"}[overall]
    return {"items": gs, "overall": overall, "overall_zh": zh}


def build_checklist(tw, twii, us, extras, quad):
    """交易員每日 Routine：盤前/盤中/盤後，帶即時數值。"""
    mac = {m["sym"]: m for m in (us or {}).get("macro", [])}
    idx = {i["sym"]: i for i in (us or {}).get("indices", [])}

    def v_idx(sym):
        m = idx.get(sym)
        return _spct(m["chg_pct"]) if m and m["chg_pct"] is not None else "—"

    def v_mac(sym, fmt="{:.1f}"):
        m = mac.get(sym)
        return fmt.format(m["val"]) if m and m.get("val") is not None else "—"

    adr = (us or {}).get("adr") or {}
    inst3 = (extras or {}).get("inst3") or {}
    mg = (extras or {}).get("margin") or {}
    txf = (extras or {}).get("txf_foreign") or {}
    b = (tw or {}).get("breadth") or {}
    lead_names = "、".join(g["name"] for g in quad["lead"][:3]) if quad["lead"] else "—"

    pre = [
        {"id": "us_close", "label": "美股收盤：標普 / 那指 / 費半",
         "val": f"{v_idx('^GSPC')} / {v_idx('^IXIC')} / {v_idx('^SOX')}"},
        {"id": "vix", "label": "VIX 恐慌指數（>20 提高警覺）", "val": v_mac("^VIX")},
        {"id": "us10y", "label": "美債 10 年殖利率與 5 日變化",
         "val": (f"{v_mac('^TNX', '{:.2f}')}%"
                 + (f"（5日 {mac['^TNX']['chg5_abs']:+.2f}）" if mac.get("^TNX", {}).get("chg5_abs") is not None else ""))},
        {"id": "fx", "label": "美元指數 / 美元台幣（台幣貶＝外資匯出壓力）",
         "val": f"{v_mac('DX-Y.NYB')} / {v_mac('TWD=X', '{:.3f}')}"},
        {"id": "adr", "label": "TSM ADR 溢價（開盤電子股風向）",
         "val": f"{adr.get('premium', 0):+.1f}%" if adr.get("premium") is not None else "—"},
        {"id": "txf_night", "label": "台指期夜盤漲跌與價差（開盤前於期交所/看盤軟體確認）", "val": ""},
        {"id": "events", "label": "今日台美重要數據/財報時程（CPI、FOMC、非農、台積電法說等）", "val": ""},
    ]
    intr = [
        {"id": "open30", "label": "開盤 30 分量能節奏（放量急拉或急殺先看族群而非指數）", "val": ""},
        {"id": "adv", "label": "漲跌家數變化 vs 昨日（昨收：漲 {u}／跌 {d}）".format(
            u=b.get("up", "—"), d=b.get("down", "—")), "val": ""},
        {"id": "lead_cont", "label": "昨日主流族群是否延續", "val": lead_names},
        {"id": "big_small", "label": "權值 vs 中小型（加權 vs 櫃買）強弱是否分歧", "val": ""},
        {"id": "limitup", "label": "漲停家數與鎖死品質（昨日：漲停 {a}／跌停 {b}）".format(
            a=b.get("limit_up", "—"), b=b.get("limit_down", "—")), "val": ""},
    ]
    post = [
        {"id": "inst3", "label": "三大法人買賣超（外資/投信/自營）",
         "val": (f"{inst3.get('total') or 0:+,.0f} 億（外 {inst3.get('foreign') or 0:+,.0f}／投 "
                 f"{inst3.get('trust') or 0:+,.0f}／自 {inst3.get('dealer') or 0:+,.0f}）"
                 if inst3.get("total") is not None else "—")},
        {"id": "margin", "label": "融資融券增減（融資餘額 {b} 億）".format(
            b=f"{mg.get('fin_bal'):,.0f}" if mg.get("fin_bal") is not None else "—"),
         "val": (f"融資 {mg.get('fin_chg'):+,.1f} 億／融券 {mg.get('short_chg'):+,.0f} 張"
                 if mg.get("fin_chg") is not None else "—")},
        {"id": "txf", "label": "外資台指期淨未平倉",
         "val": f"{txf.get('net_oi'):+,.0f} 口" if txf.get("net_oi") is not None else "—"},
        {"id": "chuzhi", "label": "處置/注意股名單更新（見處置股專區）", "val": "", "href": "chuzhi.html"},
        {"id": "review", "label": "更新族群觀察清單：主流延續？底部轉強族群加入追蹤？", "val": ""},
        {"id": "plan", "label": "寫好明日劇本：多方/空方各自的觸發條件與對應動作", "val": ""},
    ]
    return {"pre": pre, "intraday": intr, "post": post}


# ============================================================
#  DEMO 假資料（離線驗證前端）
# ============================================================
def demo_con():
    random.seed(42)
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE price(stock_id TEXT, date TEXT, open REAL, high REAL, "
                "low REAL, close REAL, volume REAL, amount REAL, PRIMARY KEY(stock_id,date))")
    con.execute("CREATE TABLE stock(stock_id TEXT PRIMARY KEY, name TEXT, market TEXT)")
    con.execute("CREATE TABLE inst(stock_id TEXT, date TEXT, foreign_lots REAL, "
                "trust_lots REAL, dealer_lots REAL, total_lots REAL, PRIMARY KEY(stock_id,date))")
    con.execute("CREATE TABLE industry(stock_id TEXT PRIMARY KEY, category TEXT)")

    d0 = dt.date(2026, 1, 2)
    dates = []
    d = d0
    while len(dates) < 130:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += dt.timedelta(days=1)

    demo_groups = [
        ("電子上游-IC設計", 1.5, 8), ("電子上游-半導體製造", 1.2, 6), ("AI伺服器", 2.0, 8),
        ("散熱模組", 1.8, 6), ("重電/電網", 0.9, 5), ("光通訊", 2.2, 6),
        ("金融-銀行", 0.3, 8), ("航運-貨櫃", -0.8, 5), ("生技-新藥", -0.5, 6),
        ("塑化", -1.0, 5), ("觀光餐飲", 0.2, 5), ("軍工/無人機", 1.1, 6),
        ("記憶體", 2.5, 5), ("面板", -0.2, 4), ("營建資產", 0.5, 6),
        ("電子下游-EMS", 1.4, 5), ("鋼鐵", -0.6, 4), ("水泥", -0.3, 3),
    ]
    bottom_turn = {"面板", "觀光餐飲"}     # 走空後近 5 日轉強＋法人回補（驗證『底部起漲』象限）
    sid = 1101
    gap_idx = 0
    for label, drift, n in demo_groups:
        for k in range(n):
            s = str(sid); sid += 7
            px = random.uniform(20, 600)
            base_amt = random.uniform(1, 40) * 1e8
            con.execute("INSERT INTO stock VALUES (?,?,?)",
                        (s, f"示範{s}", "上市" if random.random() < 0.7 else "上櫃"))
            con.execute("INSERT INTO industry VALUES (?,?)", (s, label))
            phase = random.uniform(0, 6.28)
            nlast = len(dates) - 1
            # 每 4 檔挑一檔製造「停牌缺口」：跳過部分交易日 → 真實資料常見的 None gap，
            # 用來重現並驗證 _pos()/max()/min() 的 None-safe 修復。
            gap_idx += 1
            gap_days = set()
            if gap_idx % 4 == 0:
                gap_days = set(random.sample(range(10, nlast - 3), 12))
            for i, dd in enumerate(dates):
                if i in gap_days:
                    continue          # 該日無任何 price/inst 資料
                if label in bottom_turn:
                    r = (random.gauss(2.5, 0.6) if i > nlast - 5
                         else random.gauss(-0.6, 0.8)) / 100.0
                else:
                    wave = math.sin(i / 17.0 + phase) * 0.8
                    r = random.gauss(drift * 0.05 + wave * 0.15, 1.6) / 100.0
                    if i == nlast:           # 最後一天放大族群 drift 做出當日主軸
                        r = random.gauss(drift, 1.0) / 100.0
                px = max(px * (1 + r), 1.0)
                hi, lo = px * (1 + abs(random.gauss(0, 0.008))), px * (1 - abs(random.gauss(0, 0.008)))
                boost = 1.7 if (i > nlast - 6 and (drift > 1 or label in bottom_turn)) else 1.0
                amt = base_amt * random.uniform(0.5, 1.8) * boost
                vol = amt / px
                con.execute("INSERT INTO price VALUES (?,?,?,?,?,?,?,?)",
                            (s, dd, px, hi, lo, px, vol, amt))
                if i >= len(dates) - 45:
                    tl = random.gauss(drift * 40, 300)
                    if label in bottom_turn:
                        tl = random.gauss(500, 150) if i > nlast - 5 else random.gauss(-150, 150)
                    con.execute("INSERT INTO inst VALUES (?,?,?,?,?,?)",
                                (s, dd, tl * 0.6, tl * 0.3, tl * 0.1, tl))
    con.commit()
    return con, dates


def demo_us():
    random.seed(7)
    out = {}
    d0 = dt.date(2026, 1, 2)
    dates = []
    d = d0
    while len(dates) < 129:      # 與台股 demo 對齊，美股少一天（模擬時差）
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += dt.timedelta(days=1)
    for sym in all_us_symbols():
        base = {"^GSPC": 6800, "^IXIC": 23500, "^DJI": 46500, "^SOX": 6200, "^RUT": 2400,
                "^N225": 43000, "^KS11": 3200, "^TWII": 24500, "^TWOII": 260,
                "^VIX": 16, "^TNX": 4.2, "DX-Y.NYB": 102, "TWD=X": 30.8,
                "CL=F": 72, "GC=F": 3400, "BTC-USD": 105000,
                "TSM": 240, "2330.TW": 1200}.get(sym, random.uniform(30, 300))
        drift = random.uniform(-0.1, 0.15)
        closes, highs, lows = [], [], []
        px = base * random.uniform(0.85, 0.95)
        for i in range(len(dates)):
            px = max(px * (1 + random.gauss(drift, 1.1) / 100.0), 0.5)
            closes.append(px)
            highs.append(px * 1.006)
            lows.append(px * 0.994)
        # 讓最後一天有明確方向
        closes[-1] = closes[-2] * (1 + random.gauss(drift * 3, 0.8) / 100.0)
        out[sym] = {"dates": dates, "closes": closes, "highs": highs, "lows": lows}
    return out


def demo_extras(date):
    return {"date": date,
            "inst3": {"date": date, "foreign": 182.3, "trust": 35.6, "dealer": -12.4, "total": 205.5},
            "margin": {"date": date, "fin_bal": 3251.7, "fin_chg": 18.4, "fin_unit": "億",
                       "short_bal": 512340, "short_chg": -8123},
            "txf_foreign": {"date": date, "net_oi": -18432, "long_oi": 41230, "short_oi": 59662}}


# ============================================================
#  Payload 組裝
# ============================================================
def _safe(fn, fallback, label):
    """呼叫 fn()，任何例外都吞掉並回 fallback，讓單一區塊失敗不牽連整頁。"""
    try:
        return fn()
    except Exception as e:
        import traceback
        print(f"[{label}] 失敗，該區塊以暫缺呈現：{e}")
        traceback.print_exc()
        return fallback


def build_payload(demo=False, no_us=False):
    flags = {"demo": demo, "tw_ok": False, "us_ok": False}

    # ---- 台股（讀 FinMind 建置的 twstock.db，與其他分頁同源）----
    tw = None
    extras = {}
    if demo:
        con, _ = demo_con()
        tw = _safe(lambda: tw_load(con), None, "tw_load")
        extras = demo_extras(tw["date"]) if tw else {}
        con.close()
    elif os.path.exists(DB_PATH):
        con = sqlite3.connect(DB_PATH)
        try:
            tw = _safe(lambda: tw_load(con), None, "tw_load")
        finally:
            con.close()
        extras = _safe(load_extras, {}, "load_extras")
    flags["tw_ok"] = tw is not None

    # ---- 美股 / 全球（Stooq 主・yfinance 備援）----
    us_raw = {}
    if demo:
        us_raw = demo_us()
    elif not no_us:
        # 抓 10 個月日線；起始日以今天回推
        start = (dt.datetime.now(TPE_TZ).date() - dt.timedelta(days=320)).strftime("%Y%m%d")
        us_raw = _safe(lambda: fetch_us(all_us_symbols(), start), {}, "fetch_us")
        # ^TWII 校驗：Yahoo 對此類非美股指數常「批次抓取有回傳、但少最新一天」──不是完全失敗，
        # fetch_us 的『整檔缺席才退回 Stooq』邏輯抓不到這種悄悄卡在前一天的情況。
        # 用 twstock.db 的真實最新交易日(tw['date'])核對，落後時專門用 Stooq(^twse) 補到最新。
        if tw and tw.get("date") and us_raw.get("^TWII"):
            s = us_raw["^TWII"]
            last_yf = s["dates"][-1] if s.get("dates") else None
            if last_yf and last_yf < tw["date"]:
                fresher = _safe(lambda: _fetch_stooq(["^TWII"], start).get("^TWII"), None, "twii_stooq_refresh")
                if fresher and fresher.get("dates") and fresher["dates"][-1] > last_yf:
                    print(f"  [加權指數] yfinance 停在 {last_yf}，改用 Stooq 補到 {fresher['dates'][-1]}")
                    us_raw["^TWII"] = fresher
    us = _safe(lambda: us_build(us_raw), None, "us_build") if us_raw else None
    flags["us_ok"] = us is not None

    # ---- 加權指數（Stooq ^twse / yfinance ^TWII，補均線）----
    twii = None
    if us_raw.get("^TWII"):
        s = us_raw["^TWII"]
        twii = _safe(lambda: _series_metrics(s), None, "twii")
        if twii:
            twii["ma5"] = _r(_ma(s["closes"], 5), 0)
            twii["ma20"] = _r(_ma(s["closes"], 20), 0)
            twii["ma60"] = _r(_ma(s["closes"], 60), 0)
    twoii = _safe(lambda: _series_metrics(us_raw["^TWOII"]), None, "twoii") if us_raw.get("^TWOII") else None

    # ---- 分析（各自 _safe，互不牽連）----
    quad = _safe(lambda: tw_quadrants(tw["groups"]), {"lead": [], "lag": [], "bottom": [], "strong": []},
                 "tw_quadrants") if tw else {"lead": [], "lag": [], "bottom": [], "strong": []}
    flow = _safe(lambda: tw_group_flow(tw["groups"]), {"in": [], "out": []}, "tw_group_flow") \
        if tw else {"in": [], "out": []}
    tw_paras, tw_risks, tw_light = (_safe(lambda: tw_comment(tw, twii, extras, quad),
                                          (["台股研判暫缺。"], [], "neutral"), "tw_comment")
                                    if tw else (["台股資料暫缺（twstock.db 未就緒）。"], [], "neutral"))
    us_paras, us_risks, us_light = _safe(lambda: us_comment(us), (["美股資料暫缺。"], [], "neutral"), "us_comment")
    gauges = _safe(lambda: build_gauges(tw, twii, us, extras),
                   {"items": [], "overall": "yellow", "overall_zh": "風險資料暫缺"}, "build_gauges")
    checklist = _safe(lambda: build_checklist(tw, twii, us, extras, quad),
                      {"pre": [], "intraday": [], "post": []}, "build_checklist")

    # ---- 頭條一句話 ----
    zh_light = {"bull": "偏多", "bear": "偏空", "neutral": "中性"}
    heads = []
    if tw:
        heads.append(f"台股{zh_light[tw_light]}")
        if quad["lead"]:
            heads.append(f"主軸：{quad['lead'][0]['name']}")
    if us:
        heads.append(f"美股{zh_light[us_light]}")
    heads.append({"green": "風險綠燈", "yellow": "風險黃燈", "red": "風險紅燈"}[gauges["overall"]])
    headline = "・".join(heads)

    now = dt.datetime.now(TPE_TZ)
    payload = {
        "gen_time": now.strftime("%Y-%m-%d %H:%M"),
        "tw_date": tw["date"] if tw else None,
        "tw_date_zh": _zh_date(tw["date"]) if tw else "—",
        "us_date": us.get("date") if us else None,
        "us_date_zh": _zh_date(us.get("date")) if us else "—",
        "flags": flags,
        "summary": {"tw_light": tw_light, "us_light": us_light,
                    "risk_light": gauges["overall"], "headline": headline},
        "twii": twii, "twoii": twoii,
        "tw": ({
            "amt_today": tw["amt_today"], "amt_avg20": tw["amt_avg20"],
            "amt_spark": [_r(x, 0) for x in tw["amt_daily"][-SPARK_N:]],
            "amt_split": tw["amt_split"],
            "breadth": tw["breadth"],
            "inst_days": tw["inst_days"][-INST_BAR_DAYS:],
            "inst_sum5": tw["inst_sum5"], "inst_sum20": tw["inst_sum20"],
            "quad": quad, "flow": flow,
            "comment": tw_paras, "risks": tw_risks,
        } if tw else None),
        "extras": {"inst3": (extras or {}).get("inst3"),
                   "margin": (extras or {}).get("margin"),
                   "txf": (extras or {}).get("txf_foreign")},
        "us": (dict(us, comment=us_paras, risks=us_risks) if us else None),
        "gauges": gauges,
        "checklist": checklist,
    }
    return payload


# ============================================================
#  HTML 模板（單檔自包含・極簡・行動優先・紅漲綠跌）
# ============================================================
TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#000000">
<title>市場分析｜台美股每日盤勢</title>
<style>
  :root{
    --bg:#000000; --card:#121214; --card2:#1b1b1f; --border:#2a2a2f;
    --text:#f0f1f3; --muted:#9a9aa2; --dim:#67676e;
    --amber:#ffcf3a; --up:#fb3b41; --down:#1ec77a;
    --blue:#5aa9ff; --purple:#b794ff;
    --green:#1ec77a; --yellow:#ffcf3a; --red:#fb3b41;
  }
  *{box-sizing:border-box;}
  html{-webkit-text-size-adjust:100%;}
  body{margin:0; background:var(--bg); color:var(--text);
    font-family:system-ui,-apple-system,"PingFang TC","Noto Sans TC","Microsoft JhengHei","Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased; font-size:14px; line-height:1.55;
    padding:14px 12px 48px; padding-top:calc(14px + env(safe-area-inset-top));}
  .wrap{max-width:1080px; margin:0 auto;}
  a{color:var(--blue); text-decoration:none;}
  .num,.v{font-variant-numeric:tabular-nums;}
  .up{color:var(--up);} .dn{color:var(--down);} .fl{color:var(--muted);}

  header{display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 14px; margin-bottom:4px;}
  header h1{font-size:19px; margin:0; letter-spacing:.06em; font-weight:800;}
  header .dates{color:var(--muted); font-size:12.5px;}
  header nav{margin-left:auto; font-size:12.5px; display:flex; gap:12px;}

  .strip{display:flex; flex-wrap:wrap; align-items:center; gap:8px 14px;
    border:1px solid var(--border); background:var(--card); border-radius:10px;
    padding:9px 12px; margin:10px 0 22px; font-size:13px;}
  .dot{display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; vertical-align:1px;}
  .dot.bull,.dot.green{background:var(--up);} .dot.bear{background:var(--down);}
  .dot.neutral,.dot.yellow{background:var(--amber);} .dot.red{background:var(--red);}
  .dot.green{background:var(--green);}
  .strip .hd{color:var(--muted); margin-left:auto; font-size:12px;}

  section{margin:0 0 30px;}
  h2{font-size:13px; letter-spacing:.22em; color:var(--muted); font-weight:700;
     text-transform:uppercase; margin:0 0 12px; display:flex; align-items:center; gap:8px;}
  h2::after{content:""; flex:1; height:1px; background:var(--border);}
  .note{color:var(--dim); font-size:11.5px; margin-top:8px;}

  .grid2{display:grid; grid-template-columns:1fr; gap:12px;}
  .grid3{display:grid; grid-template-columns:1fr; gap:12px;}
  @media(min-width:760px){ .grid2{grid-template-columns:1fr 1fr;} .grid3{grid-template-columns:repeat(3,1fr);} }
  .grid2>*,.grid3>*,.quadgrid>*,.tilegrid>*{min-width:0;}

  .card{border:1px solid var(--border); background:var(--card); border-radius:12px; padding:13px 14px;}
  .card h3{margin:0 0 9px; font-size:13px; color:var(--muted); font-weight:700; letter-spacing:.04em;}

  /* 指數卡 */
  .bignum{font-size:27px; font-weight:800; letter-spacing:.01em;}
  .idxrow{display:flex; align-items:center; gap:12px; flex-wrap:wrap;}
  .idxmeta{color:var(--muted); font-size:12px; display:flex; gap:10px; flex-wrap:wrap; margin-top:7px;}
  .tilegrid{display:grid; grid-template-columns:repeat(2,1fr); gap:9px;}
  @media(min-width:560px){ .tilegrid{grid-template-columns:repeat(3,1fr);} }
  @media(min-width:900px){ .tilegrid{grid-template-columns:repeat(5,1fr);} }
  .tile{border:1px solid var(--border); background:var(--card); border-radius:10px; padding:9px 11px; min-width:0;}
  .tile .t{color:var(--muted); font-size:11.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .tile .p{font-size:16.5px; font-weight:800; margin-top:2px;}
  .tile .c{font-size:12px; margin-top:1px;}
  .tile svg{display:block; margin-top:6px; width:100%; height:26px;}

  .breadthbar{display:flex; height:9px; border-radius:5px; overflow:hidden; margin:7px 0 5px; background:var(--card2);}
  .breadthbar i{display:block; height:100%;}
  .bmeta{display:flex; justify-content:space-between; color:var(--muted); font-size:11.5px;}

  /* 研判 */
  .comment p{margin:0 0 9px; color:var(--text);}
  .comment p:last-child{margin-bottom:0;}
  .risks{margin:10px 0 0; padding:0; list-style:none;}
  .risks li{position:relative; padding-left:16px; color:var(--amber); font-size:12.5px; margin-top:5px;}
  .risks li::before{content:"⚠"; position:absolute; left:0; font-size:11px;}

  /* 橫向長條列 */
  .hrow{display:grid; grid-template-columns:minmax(72px,1.1fr) 2fr auto; align-items:center;
        gap:8px; padding:4.5px 0; font-size:12.5px;}
  .hrow .nm{color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .bar{height:13px; border-radius:3px; position:relative; background:var(--card2); overflow:hidden; display:block;}
  .bar i{position:absolute; top:0; bottom:0;}
  .hrow .vv{min-width:60px; text-align:right; font-weight:700;}

  /* 法人堆疊圖 */
  .legend{display:flex; gap:12px; color:var(--muted); font-size:11.5px; margin-top:7px; flex-wrap:wrap;}
  .legend i{display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:4px; vertical-align:-1px;}

  /* 表格 */
  .mini{width:100%; border-collapse:collapse; font-size:12.5px;}
  .mini th{color:var(--dim); font-weight:600; text-align:right; padding:4px 6px;
           border-bottom:1px solid var(--border); font-size:11.5px; white-space:nowrap;}
  .mini th:first-child,.mini td:first-child{text-align:left; padding-left:0;}
  .mini td{padding:5px 6px; text-align:right; border-bottom:1px solid rgba(42,42,47,.5); white-space:nowrap;}
  .mini td:first-child{white-space:normal;}
  .mini tr:last-child td{border-bottom:none;}
  .tablewrap{overflow-x:auto; -webkit-overflow-scrolling:touch;}

  /* 族群雷達 */
  .quadgrid{display:grid; grid-template-columns:1fr; gap:12px;}
  @media(min-width:860px){ .quadgrid{grid-template-columns:1fr 1fr;} }
  .quad h3{display:flex; align-items:baseline; gap:7px;}
  .quad h3 .sub2{color:var(--dim); font-weight:400; font-size:11px; letter-spacing:0;}
  details.grow{border-top:1px solid rgba(42,42,47,.55);}
  details.grow:first-of-type{border-top:none;}
  details.grow summary{list-style:none; cursor:pointer; padding:7.5px 0; display:block;}
  details.grow summary::-webkit-details-marker{display:none;}
  .grow .l1{display:flex; align-items:baseline; gap:8px;}
  .grow .gname{font-weight:700; font-size:13.5px; flex:1; min-width:0;
               white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .grow .r1{font-weight:800; font-size:14px;}
  .grow .l2{display:flex; gap:10px; flex-wrap:wrap; color:var(--muted); font-size:11.5px; margin-top:3px; align-items:center;}
  .posbar{display:inline-block; width:44px; height:5px; border-radius:3px; background:var(--card2);
          position:relative; vertical-align:1px;}
  .posbar i{position:absolute; left:0; top:0; bottom:0; border-radius:3px; background:var(--blue);}
  .chips{display:flex; flex-wrap:wrap; gap:6px; padding:2px 0 10px;}
  .chip{border:1px solid var(--border); background:var(--card2); border-radius:7px;
        padding:3px 8px; font-size:11.5px; color:var(--text); white-space:nowrap;}
  .chip b{font-weight:700; margin-left:4px;}
  .empty{color:var(--dim); font-size:12.5px; padding:8px 0;}

  /* 監控清單 */
  .chk{list-style:none; margin:0; padding:0;}
  .chk li{border-top:1px solid rgba(42,42,47,.55); padding:7px 0;}
  .chk li:first-child{border-top:none;}
  .chk label{display:flex; gap:9px; align-items:flex-start; cursor:pointer;}
  .chk input{margin-top:3px; accent-color:var(--amber); width:15px; height:15px; flex:none;}
  .chk .lb{flex:1; font-size:12.5px;}
  .chk .val{display:block; color:var(--blue); font-size:12px; margin-top:1px; font-weight:700;}
  .chk input:checked ~ .lb{color:var(--dim); text-decoration:line-through;}
  .chk input:checked ~ .lb .val{color:var(--dim);}

  /* 風險儀表 */
  .ggrid{display:grid; grid-template-columns:repeat(2,1fr); gap:9px;}
  @media(min-width:760px){ .ggrid{grid-template-columns:repeat(3,1fr);} }
  .gauge{border:1px solid var(--border); background:var(--card); border-radius:10px; padding:10px 12px;}
  .gauge .t{color:var(--muted); font-size:11.5px;}
  .gauge .p{font-size:16px; font-weight:800; margin:3px 0 2px;}
  .gauge .h{color:var(--dim); font-size:10.5px; line-height:1.4;}
  .overall{border-radius:10px; padding:10px 13px; margin-bottom:11px; font-weight:700; font-size:13px;
           border:1px solid var(--border); background:var(--card);}
  .overall.green{border-color:rgba(30,199,122,.5);} .overall.yellow{border-color:rgba(255,207,58,.55);}
  .overall.red{border-color:rgba(251,59,65,.6);}

  footer{color:var(--dim); font-size:11px; border-top:1px solid var(--border);
         padding-top:14px; margin-top:8px; line-height:1.8;}
  .warnbar{border:1px solid rgba(255,207,58,.5); color:var(--amber); background:rgba(255,207,58,.06);
           border-radius:10px; padding:8px 12px; font-size:12.5px; margin:0 0 14px;}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>📊 市場分析</h1>
    <div class="dates num" id="hdrDates">—</div>
    <nav><a href="index.html">← 主看板</a><a href="hui.html">🐉 輝哥選股</a><a href="chuzhi.html">處置股</a></nav>
  </header>

  <div class="strip" id="strip"></div>
  <div id="warn"></div>

  <section id="secOverview">
    <h2>今日總覽</h2>
    <div class="grid2" id="ovTop"></div>
    <div style="height:12px"></div>
    <div class="tilegrid" id="usIdxTiles"></div>
    <div style="height:12px"></div>
    <div class="tilegrid" id="macroTiles"></div>
    <div class="note">台股為本日收盤；美股/總經為最近收盤交易日（台北下午更新時通常為前一晚）。紅漲綠跌。</div>
  </section>

  <section id="secComment">
    <h2>盤勢研判</h2>
    <div class="grid2">
      <div class="card"><h3>台股</h3><div class="comment" id="twComment"></div><ul class="risks" id="twRisks"></ul></div>
      <div class="card"><h3>美股</h3><div class="comment" id="usComment"></div><ul class="risks" id="usRisks"></ul></div>
    </div>
    <div class="note">依當日量價/廣度/法人/利率等訊號由規則自動生成，供複盤起點，非投資建議。</div>
  </section>

  <section id="secFlow">
    <h2>資金流向</h2>
    <div class="grid2">
      <div class="card">
        <h3>三大法人買賣超（近 20 日・億）</h3>
        <div id="instChart"></div>
        <div class="legend"><span><i style="background:var(--blue)"></i>外資</span>
          <span><i style="background:var(--amber)"></i>投信</span>
          <span><i style="background:var(--purple)"></i>自營</span></div>
        <div class="idxmeta" id="instSums"></div>
      </div>
      <div class="card"><h3>籌碼與部位</h3><div id="chipTiles" class="tilegrid" style="grid-template-columns:repeat(2,1fr)"></div></div>
      <div class="card"><h3>族群資金淨流入 TOP（近5日法人・億）</h3><div id="flowIn"></div></div>
      <div class="card"><h3>族群資金淨流出 TOP（近5日法人・億）</h3><div id="flowOut"></div></div>
    </div>
    <div style="height:12px"></div>
    <div class="grid2">
      <div class="card"><h3>美股 11 大類股 ETF 強弱</h3><div class="tablewrap" id="usSectors"></div></div>
      <div class="card"><h3>風險偏好比率（5日／20日 相對報酬差，百分點）</h3><div id="usRatios"></div>
        <div class="note">正值（紅）＝偏 Risk-on、負值（綠）＝偏 Risk-off（依各指標含義解讀）。</div></div>
    </div>
  </section>

  <section id="secTwQuad">
    <h2>台股族群雷達</h2>
    <div class="quadgrid" id="twQuad"></div>
    <div class="note">以概念族群聚合（成交值加權）。位階＝現價在近 120 日高低區間位置；法5/法20＝三大法人近 5/20 日淨買賣（估算，億）。點族群展開成分股、點代號開 K 線。</div>
  </section>

  <section id="secUsQuad">
    <h2>美股族群雷達</h2>
    <div class="quadgrid" id="usQuad"></div>
    <div class="note">以類股/主題 ETF 為族群代理；RS＝相對 SPY 的同期超額報酬。點 ETF 可開 Yahoo 報價頁。</div>
  </section>

  <section id="secChk">
    <h2>每日監控清單</h2>
    <div class="grid3">
      <div class="card"><h3>盤前（08:00–09:00）</h3><ul class="chk" id="chkPre"></ul></div>
      <div class="card"><h3>盤中（09:00–13:30）</h3><ul class="chk" id="chkIn"></ul></div>
      <div class="card"><h3>盤後（14:30–）</h3><ul class="chk" id="chkPost"></ul></div>
    </div>
    <div class="note">勾選狀態存於此裝置（localStorage），資料日更新後自動重置。</div>
  </section>

  <section id="secGauge">
    <h2>風險儀表</h2>
    <div class="overall" id="gaugeOverall"></div>
    <div class="ggrid" id="gauges"></div>
  </section>

  <footer>
    產生時間 <span class="num" id="ftGen">—</span>（台北）・資料來源：<a href="https://finmindtrade.com" target="_blank" rel="noopener" style="color:inherit; text-decoration:underline">FinMind</a>（台股價量/法人/處置）、台灣證交所、期交所、Yahoo Finance（美股/總經）。<br>
    本頁為程式化統計與規則式研判，僅供研究參考，非投資建議；數據可能因來源延遲或缺漏而不完整。
  </footer>
</div>

<script id="PAYLOAD" type="application/json">__PAYLOAD__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById('PAYLOAD').textContent);

/* ---------- 小工具 ---------- */
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const isNum = v => typeof v === "number" && isFinite(v);
function fmt(v, dec, unit){ return isNum(v) ? v.toLocaleString("en-US",{minimumFractionDigits:dec??1, maximumFractionDigits:dec??1}) + (unit||"") : "—"; }
function pctHtml(v, dec){ if(!isNum(v)) return '<span class="fl">—</span>';
  dec = dec??2;
  if(Math.abs(v) < 0.5*Math.pow(10,-dec)) v = 0;
  const cls = v > 0 ? "up" : (v < 0 ? "dn" : "fl");
  return `<span class="${cls}">${v>0?"+":""}${v.toFixed(dec)}%</span>`; }
function amtHtml(v, unit){ if(!isNum(v)) return '<span class="fl">—</span>';
  const cls = v > 0 ? "up" : (v < 0 ? "dn" : "fl");
  return `<span class="${cls}">${v>0?"+":""}${fmt(v, Math.abs(v)>=100?0:1)}${unit||""}</span>`; }
function lightZh(l){ return {bull:"偏多", bear:"偏空", neutral:"中性", green:"綠燈", yellow:"黃燈", red:"紅燈"}[l] || l; }

function spark(arr, w, h, opt){
  if(!arr || arr.length < 2) return "";
  w = w||120; h = h||26; opt = opt||{};
  const xs = arr.filter(isNum); if(xs.length < 2) return "";
  const mn = Math.min(...xs), mx = Math.max(...xs), rg = (mx-mn)||1;
  const pts = arr.map((v,i)=>{ if(!isNum(v)) return null;
    return [ (i/(arr.length-1))*w, h-2-((v-mn)/rg)*(h-4) ]; }).filter(Boolean);
  const dpath = pts.map((p,i)=> (i?"L":"M")+p[0].toFixed(1)+" "+p[1].toFixed(1)).join("");
  const color = opt.color || (arr[arr.length-1] >= arr[0] ? "var(--up)" : "var(--down)");
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">`+
    `<path d="${dpath}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

/* ---------- 頁首 / 結論條 ---------- */
(function(){
  $("hdrDates").textContent = `台股 ${D.tw_date_zh}・美股 ${D.us_date_zh}`;
  $("ftGen").textContent = D.gen_time;
  const s = D.summary;
  $("strip").innerHTML =
    `<span><i class="dot ${s.tw_light}"></i>台股 ${lightZh(s.tw_light)}</span>`+
    `<span><i class="dot ${s.us_light}"></i>美股 ${lightZh(s.us_light)}</span>`+
    `<span><i class="dot ${s.risk_light}"></i>風險${lightZh(s.risk_light)}</span>`+
    `<span class="hd">${esc(s.headline)}</span>`;
  const warns = [];
  if(D.flags.demo) warns.push("目前為 DEMO 合成資料，僅供介面預覽。");
  if(!D.flags.tw_ok) warns.push("台股資料暫缺（等待每日排程建立 twstock.db）。");
  if(!D.flags.us_ok) warns.push("美股資料暫缺（yfinance 未能連線）。");
  if(warns.length) $("warn").innerHTML = `<div class="warnbar">${warns.map(esc).join(" ")}</div>`;
})();

/* ---------- 今日總覽 ---------- */
(function(){
  const tw = D.tw, twii = D.twii, b = tw && tw.breadth;
  let leftHtml = "<h3>台股</h3>";
  if(twii){
    leftHtml += `<div class="idxrow"><div><div class="bignum num">${fmt(twii.close,0)}</div>`+
      `<div>${pctHtml(twii.chg_pct)}　<span class="fl" style="font-size:12px">加權指數</span></div></div>`+
      `<div style="flex:1;min-width:110px;max-width:220px">${spark(twii.spark, 200, 44)}</div></div>`;
    const ms = [];
    if(isNum(twii.ma20)) ms.push(`月線 ${fmt(twii.ma20,0)}${twii.close>=twii.ma20?"↑":"↓"}`);
    if(isNum(twii.ma60)) ms.push(`季線 ${fmt(twii.ma60,0)}${twii.close>=twii.ma60?"↑":"↓"}`);
    if(isNum(twii.pos)) ms.push(`位階 ${twii.pos}%`);
    if(D.twoii && isNum(D.twoii.chg_pct)) ms.push(`櫃買 ${D.twoii.chg_pct>0?"+":""}${D.twoii.chg_pct.toFixed(2)}%`);
    leftHtml += `<div class="idxmeta num">${ms.join("　")}</div>`;
  } else {
    leftHtml += `<div class="empty">加權指數資料暫缺</div>`;
  }
  if(tw){
    const ratio = (isNum(tw.amt_today)&&isNum(tw.amt_avg20)&&tw.amt_avg20>0)? tw.amt_today/tw.amt_avg20 : null;
    leftHtml += `<div class="idxmeta num" style="margin-top:10px">成交值 <b style="color:var(--text)">${fmt(tw.amt_today,0)} 億</b>`+
      (ratio?`（20日均 ${fmt(tw.amt_avg20,0)} 億・量比 ${ratio.toFixed(2)}）`:"")+`</div>`;
    if(b){
      const tot=(b.up||0)+(b.down||0)+(b.flat||0);
      if(tot>0){
        const w=k=>((b[k]||0)/tot*100).toFixed(1)+"%";
        leftHtml += `<div class="breadthbar"><i style="width:${w("up")};background:var(--up)"></i>`+
          `<i style="width:${w("flat")};background:var(--dim)"></i>`+
          `<i style="width:${w("down")};background:var(--down)"></i></div>`+
          `<div class="bmeta num"><span class="up">漲 ${b.up}（漲停 ${b.limit_up}）</span>`+
          `<span class="dn">跌 ${b.down}（跌停 ${b.limit_down}）</span></div>`;
      }
      const meta=[];
      if(isNum(b.pct_ab20)) meta.push(`站上月線 ${b.pct_ab20}%`);
      if(isNum(b.pct_ab60)) meta.push(`站上季線 ${b.pct_ab60}%`);
      meta.push(`60日新高 ${b.nh60??"—"} 檔`, `新低 ${b.nl60??"—"} 檔`);
      leftHtml += `<div class="idxmeta num">${meta.join("　")}</div>`;
    }
  } else leftHtml += `<div class="empty">台股統計暫缺</div>`;

  let rightHtml = "<h3>亞股 & 台股連動</h3>";
  const rows=[];
  (D.us && D.us.asia || []).forEach(m=> rows.push([m.name, fmt(m.close,0), m.chg_pct]));
  if(D.us && D.us.adr) { const a=D.us.adr;
    rows.push([`TSM ADR 溢價 <span class="fl" style="font-size:11px">ADR ${fmt(a.tsm,1)}＄／2330 ${fmt(a.px2330,0)}</span>`,
      (isNum(a.premium)?(a.premium>0?"+":"")+a.premium.toFixed(1)+"%":"—"), null, true]); }
  const mac=(D.us&&D.us.macro||[]).find(m=>m.sym==="TWD=X");
  if(mac) rows.push(["美元/台幣", fmt(mac.val,3), mac.chg_pct]);
  if(rows.length){
    rightHtml += `<table class="mini"><tbody>`+rows.map(r=>
      `<tr><td>${r[3]?r[0]:esc(r[0])}</td><td class="num" style="font-weight:700">${r[1]}</td><td>${r[2]==null?"":pctHtml(r[2])}</td></tr>`).join("")+`</tbody></table>`;
  } else rightHtml += `<div class="empty">暫無資料</div>`;
  if(D.us && D.us.mega && D.us.mega.length){
    rightHtml += `<h3 style="margin-top:12px">美股權值股（最近收盤）</h3><div class="chips">`+
      D.us.mega.map(m=>`<span class="chip num">${esc(m.sym)}<b>${isNum(m.ret1)?((m.ret1>0?"+":"")+m.ret1.toFixed(1)+"%"):"—"}</b></span>`
        .replace("<b>", `<b class="${(m.ret1||0)>0?"up":(m.ret1||0)<0?"dn":"fl"}">`)).join("")+`</div>`;
  }
  $("ovTop").innerHTML = `<div class="card">${leftHtml}</div><div class="card">${rightHtml}</div>`;

  const tiles=(D.us&&D.us.indices||[]).map(m=>
    `<div class="tile"><div class="t">${esc(m.name)}</div><div class="p num">${fmt(m.close,0)}</div>`+
    `<div class="c">${pctHtml(m.chg_pct)}<span class="fl num" style="margin-left:6px">5日 ${isNum(m.ret5)?(m.ret5>0?"+":"")+m.ret5.toFixed(1)+"%":"—"}</span></div>${spark(m.spark)}</div>`).join("");
  $("usIdxTiles").innerHTML = tiles || `<div class="empty">美股指數暫缺</div>`;

  const mtiles=(D.us&&D.us.macro||[]).map(m=>{
    const dec = m.sym==="^TNX"?2 : (m.val>=1000?0 : (m.val>=100?1:2));
    let chg = "";
    if(m.sym==="^TNX" && isNum(m.chg5_abs)) chg = `<span class="fl num">5日 ${m.chg5_abs>0?"+":""}${m.chg5_abs.toFixed(2)}</span>`;
    else chg = pctHtml(m.chg_pct);
    return `<div class="tile"><div class="t">${esc(m.name)}</div><div class="p num">${fmt(m.val,dec)}${m.unit||""}</div>`+
      `<div class="c">${chg}</div>${spark(m.spark)}</div>`;}).join("");
  $("macroTiles").innerHTML = mtiles || "";
})();

/* ---------- 盤勢研判 ---------- */
(function(){
  const put=(pid, rid, paras, risks)=>{
    $(pid).innerHTML=(paras&&paras.length?paras:["暫無資料"]).map(p=>`<p>${esc(p)}</p>`).join("");
    $(rid).innerHTML=(risks||[]).map(r=>`<li>${esc(r)}</li>`).join("");
  };
  put("twComment","twRisks", D.tw&&D.tw.comment, D.tw&&D.tw.risks);
  put("usComment","usRisks", D.us&&D.us.comment, D.us&&D.us.risks);
})();

/* ---------- 資金流向 ---------- */
(function(){
  const tw=D.tw;
  // 法人 20 日堆疊圖
  if(tw && tw.inst_days && tw.inst_days.length){
    const days=tw.inst_days, W=560, H=150, mid=H/2, n=days.length;
    const bw=Math.min(18, (W-10)/n*0.62), step=(W-10)/n;
    let maxAbs=1;
    days.forEach(d=>{ const pos=Math.max(d.f||0,0)+Math.max(d.t||0,0)+Math.max(d.d||0,0);
      const neg=Math.min(d.f||0,0)+Math.min(d.t||0,0)+Math.min(d.d||0,0);
      maxAbs=Math.max(maxAbs,pos,-neg); });
    const sc=(mid-14)/maxAbs;
    let svg=`<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto" role="img" aria-label="三大法人近20日買賣超">`;
    svg+=`<line x1="0" y1="${mid}" x2="${W}" y2="${mid}" stroke="var(--border)" stroke-width="1"/>`;
    const colors={f:"var(--blue)", t:"var(--amber)", d:"var(--purple)"};
    days.forEach((d,i)=>{
      const x=5+i*step+(step-bw)/2; let upy=mid, dny=mid;
      ["f","t","d"].forEach(k=>{ const v=d[k]||0; const h=Math.abs(v)*sc; if(h<0.3) return;
        if(v>=0){ upy-=h; svg+=`<rect x="${x.toFixed(1)}" y="${upy.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" fill="${colors[k]}"/>`; }
        else { svg+=`<rect x="${x.toFixed(1)}" y="${dny.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}" fill="${colors[k]}" opacity="0.55"/>`; dny+=h; } });
      const tot=d.tot||0;
      svg+=`<circle cx="${(x+bw/2).toFixed(1)}" cy="${(mid-tot*sc).toFixed(1)}" r="1.8" fill="${tot>=0?"var(--up)":"var(--down)"}"/>`;
      if(i===0||i===n-1||i===Math.floor(n/2)){
        svg+=`<text x="${(x+bw/2).toFixed(1)}" y="${H-2}" fill="var(--dim)" font-size="9" text-anchor="middle">${esc((d.date||"").slice(5))}</text>`; }
    });
    svg+=`</svg>`;
    $("instChart").innerHTML=svg;
    const s5=tw.inst_sum5, s20=tw.inst_sum20, seg=[];
    if(s5&&isNum(s5.tot)) seg.push(`近5日合計 ${amtHtml(s5.tot," 億").replace(/span/g,"b")}`);
    if(s20&&isNum(s20.tot)) seg.push(`近20日合計 ${amtHtml(s20.tot," 億").replace(/span/g,"b")}`);
    if(s20&&isNum(s20.t)) seg.push(`投信20日 ${amtHtml(s20.t," 億").replace(/span/g,"b")}`);
    $("instSums").innerHTML=seg.join("　")+`<span class="fl">（估算値・圓點＝當日合計）</span>`;
  } else $("instChart").innerHTML=`<div class="empty">法人資料暫缺</div>`;

  // 籌碼 tiles
  const e=D.extras||{}; const t=[];
  if(e.inst3&&isNum(e.inst3.total)) t.push(["三大法人（官方・當日）", amtHtml(e.inst3.total," 億"),
    `外 ${fmt(e.inst3.foreign,0)}／投 ${fmt(e.inst3.trust,0)}／自 ${fmt(e.inst3.dealer,0)}`]);
  if(e.margin&&isNum(e.margin.fin_chg)) t.push(["融資增減", amtHtml(e.margin.fin_chg," 億"),
    `餘額 ${fmt(e.margin.fin_bal,0)} 億`]);
  if(e.margin&&isNum(e.margin.short_chg)) t.push(["融券增減", amtHtml(e.margin.short_chg," 張"),
    `餘額 ${fmt(e.margin.short_bal,0)} 張`]);
  if(e.txf&&isNum(e.txf.net_oi)) t.push(["外資台指期淨部位", amtHtml(e.txf.net_oi," 口"),
    `多 ${fmt(e.txf.long_oi,0)}／空 ${fmt(e.txf.short_oi,0)}`]);
  $("chipTiles").innerHTML = t.length? t.map(x=>
    `<div class="tile"><div class="t">${esc(x[0])}</div><div class="p num" style="font-size:15px">${x[1]}</div><div class="c fl num">${esc(x[2])}</div></div>`).join("")
    : `<div class="empty">籌碼資料暫缺</div>`;

  // 族群資金流 in/out
  function flowRows(el, arr, posColor){
    if(!arr||!arr.length){ $(el).innerHTML=`<div class="empty">今日無明顯訊號</div>`; return; }
    const mx=Math.max(...arr.map(x=>Math.abs(x.amt||0)),0.1);
    $(el).innerHTML=arr.map(x=>{
      const w=Math.abs(x.amt||0)/mx*100;
      return `<div class="hrow"><span class="nm">${esc(x.name)}</span>`+
        `<span class="bar"><i style="left:0;width:${w.toFixed(1)}%;background:${posColor}"></i></span>`+
        `<span class="vv num">${amtHtml(x.amt)}<span class="fl" style="font-weight:400;margin-left:5px">${isNum(x.ret1)?(x.ret1>0?"+":"")+x.ret1.toFixed(1)+"%":""}</span></span></div>`;
    }).join("");
  }
  flowRows("flowIn",  tw&&tw.flow&&tw.flow.in,  "rgba(251,59,65,.75)");
  flowRows("flowOut", tw&&tw.flow&&tw.flow.out, "rgba(30,199,122,.75)");

  // 美股 11 類股
  const ss=D.us&&D.us.sectors||[];
  if(ss.length){
    const sorted=[...ss].sort((a,b)=>(b.ret1||0)-(a.ret1||0));
    const mx=Math.max(...sorted.map(s=>Math.abs(s.ret1||0)),0.5);
    $("usSectors").innerHTML=`<table class="mini"><thead><tr><th>類股</th><th style="text-align:left;width:34%">當日</th><th>5日</th><th>20日</th><th>RS20</th></tr></thead><tbody>`+
      sorted.map(s=>{
        const v=s.ret1||0, w=Math.abs(v)/mx*50;
        const bar=`<span class="bar" style="display:block;height:11px"><i style="left:${v>=0?50:50-w}%;width:${w}%;background:${v>=0?"var(--up)":"var(--down)"}"></i></span>`;
        return `<tr><td>${esc(s.name)} <span class="fl">${esc(s.sym)}</span></td><td>${bar}</td>`+
          `<td>${pctHtml(s.ret5,1)}</td><td>${pctHtml(s.ret20,1)}</td>`+
          `<td class="num ${(s.rs20||0)>0?"up":"dn"}">${isNum(s.rs20)?(s.rs20>0?"+":"")+s.rs20.toFixed(1):"—"}</td></tr>`;
      }).join("")+`</tbody></table>`;
  } else $("usSectors").innerHTML=`<div class="empty">暫無資料</div>`;

  // 風險偏好比率
  const rs=D.us&&D.us.ratios||[];
  $("usRatios").innerHTML = rs.length? rs.map(r=>{
    const cell=v=>!isNum(v)?`<span class="fl">—</span>`:
      `<span class="num" style="font-weight:700;color:${v>=0?"var(--up)":"var(--down)"}">${v>0?"+":""}${v.toFixed(1)}</span>`;
    return `<div class="hrow" style="grid-template-columns:1.4fr auto auto">`+
      `<span class="nm">${esc(r.name)}<br><span class="fl" style="font-size:10.5px">${esc(r.hint)}</span></span>`+
      `<span class="vv">5日 ${cell(r.v5)}</span><span class="vv">20日 ${cell(r.v20)}</span></div>`;
  }).join("") : `<div class="empty">暫無資料</div>`;
})();

/* ---------- 族群雷達 ---------- */
function quadCard(title, subtitle, arr, opt){
  opt=opt||{};
  let inner="";
  if(!arr||!arr.length) inner=`<div class="empty">今日無符合條件的族群</div>`;
  else inner=arr.map((g,gi)=>{
    const meta=[];
    if(isNum(g.ret5))  meta.push(`5日 ${g.ret5>0?"+":""}${g.ret5.toFixed(1)}%`);
    if(isNum(g.ret20)) meta.push(`20日 ${g.ret20>0?"+":""}${g.ret20.toFixed(1)}%`);
    if(isNum(g.pos)) meta.push(`<span class="posbar"><i style="width:${g.pos}%"></i></span> ${g.pos.toFixed(0)}%`);
    if(opt.us){ if(isNum(g.rs20)) meta.push(`RS20 ${g.rs20>0?"+":""}${g.rs20.toFixed(1)}`); }
    else {
      if(isNum(g.inst5))  meta.push(`法5 ${g.inst5>0?"+":""}${fmt(g.inst5, Math.abs(g.inst5)>=100?0:1)}億`);
      if(isNum(g.inst20)) meta.push(`法20 ${g.inst20>0?"+":""}${fmt(g.inst20, Math.abs(g.inst20)>=100?0:1)}億`);
      if(isNum(g.share)) meta.push(`佔比 ${g.share}%`);
    }
    const nameHtml = opt.us
      ? `<a href="https://finance.yahoo.com/quote/${encodeURIComponent(g.sym)}" target="_blank" rel="noopener" style="color:inherit">${esc(g.name)} <span class="fl">${esc(g.sym)}</span></a>`
      : esc(g.name)+(isNum(g.n)?` <span class="fl" style="font-weight:400;font-size:11px">${g.n}檔</span>`:"");
    const head=`<div class="l1"><span class="fl num" style="font-size:11px;width:14px">${gi+1}</span>`+
      `<span class="gname">${nameHtml}</span><span class="r1">${pctHtml(g.ret1)}</span></div>`+
      `<div class="l2 num">${meta.join("<span style='color:var(--border)'>｜</span>")}</div>`;
    if(opt.us || !g.members || !g.members.length) return `<details class="grow"><summary>${head}</summary></details>`;
    const chips=g.members.map(m=>{
      const cls=(m.ret1||0)>0?"up":(m.ret1||0)<0?"dn":"fl";
      return `<a class="chip num" href="index.html?stk=${encodeURIComponent(m.sid)}">${esc(m.sid)} ${esc(m.name)}`+
        `<b class="${cls}">${isNum(m.ret1)?(m.ret1>0?"+":"")+m.ret1.toFixed(1)+"%":"—"}</b></a>`; }).join("");
    return `<details class="grow"${gi===0&&opt.openFirst?" open":""}><summary>${head}</summary><div class="chips">${chips}</div></details>`;
  }).join("");
  return `<div class="card quad"><h3>${title} <span class="sub2">${esc(subtitle)}</span></h3>${inner}</div>`;
}
(function(){
  const q=D.tw&&D.tw.quad;
  $("twQuad").innerHTML = q? [
    quadCard("🔥 今日漲勢主軸","當日領漲、成交值≥10億", q.lead, {openFirst:true}),
    quadCard("🧊 今日跌勢主軸","當日領跌", q.lag, {}),
    quadCard("🌱 底部起漲","位階低＋5日轉強＋量增＋法人回補", q.bottom, {openFirst:true}),
    quadCard("💪 持續強勢","位階高＋20/60日多頭＋站上月線", q.strong, {}),
  ].join("") : `<div class="empty">台股資料暫缺</div>`;
  const uq=D.us&&D.us.quad;
  $("usQuad").innerHTML = uq? [
    quadCard("🔥 今日漲勢主軸","最近收盤領漲主題", uq.lead, {us:true}),
    quadCard("🧊 今日跌勢主軸","最近收盤領跌主題", uq.lag, {us:true}),
    quadCard("🌱 底部起漲","位階低＋5日轉強且跑贏大盤", uq.bottom, {us:true}),
    quadCard("💪 持續強勢","位階高＋20日RS為正", uq.strong, {us:true}),
  ].join("") : `<div class="empty">美股資料暫缺</div>`;
})();

/* ---------- 每日監控清單 ---------- */
(function(){
  const key="mkchk_"+(D.tw_date||"na");
  Object.keys(localStorage).forEach(k=>{ if(k.startsWith("mkchk_")&&k!==key) localStorage.removeItem(k); });
  let st={}; try{ st=JSON.parse(localStorage.getItem(key)||"{}"); }catch(e){}
  function render(el, arr){
    $(el).innerHTML=(arr||[]).map(it=>{
      const lb=esc(it.label)+(it.href?` <a href="${esc(it.href)}">開啟 →</a>`:"");
      const val=it.val?`<span class="val num">${esc(it.val)}</span>`:"";
      return `<li><label><input type="checkbox" data-cid="${esc(it.id)}"${st[it.id]?" checked":""}><span class="lb">${lb}${val}</span></label></li>`;
    }).join("");
  }
  render("chkPre",  D.checklist&&D.checklist.pre);
  render("chkIn",   D.checklist&&D.checklist.intraday);
  render("chkPost", D.checklist&&D.checklist.post);
  document.querySelectorAll(".chk input").forEach(cb=>cb.addEventListener("change",()=>{
    st[cb.dataset.cid]=cb.checked; localStorage.setItem(key, JSON.stringify(st)); }));
})();

/* ---------- 風險儀表 ---------- */
(function(){
  const g=D.gauges||{items:[]};
  $("gaugeOverall").className="overall "+(g.overall||"yellow");
  $("gaugeOverall").innerHTML=`<i class="dot ${g.overall}"></i>${esc(g.overall_zh||"")}`;
  $("gauges").innerHTML=(g.items||[]).map(it=>
    `<div class="gauge"><div class="t"><i class="dot ${it.light}"></i>${esc(it.name)}</div>`+
    `<div class="p num">${esc(it.val)}</div><div class="h">${esc(it.hint)}</div></div>`).join("")
    || `<div class="empty">暫無資料</div>`;
})();
</script>
</body>
</html>
"""


def render_html(payload):
    js = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                    separators=(",", ":")).replace("</", "<\\/")
    return TEMPLATE.replace("__PAYLOAD__", js)


def main():
    ap = argparse.ArgumentParser(description="台美股每日市場分析面板產生器")
    ap.add_argument("--demo", action="store_true", help="離線合成資料（驗證前端）")
    ap.add_argument("--no-us", action="store_true", help="跳過 yfinance（只出台股）")
    ap.add_argument("--out", default=os.path.join(OUT_DIR, OUT_NAME))
    args = ap.parse_args()

    try:
        payload = build_payload(demo=args.demo, no_us=args.no_us)
    except Exception as e:
        # 最後防線：任何未預期錯誤都輸出可開啟的占位頁，不讓 pipeline 掛掉
        import traceback
        traceback.print_exc()
        now = dt.datetime.now(TPE_TZ)
        payload = {
            "gen_time": now.strftime("%Y-%m-%d %H:%M"), "tw_date": None, "tw_date_zh": "—",
            "us_date": None, "us_date_zh": "—",
            "flags": {"demo": False, "tw_ok": False, "us_ok": False},
            "summary": {"tw_light": "neutral", "us_light": "neutral",
                        "risk_light": "yellow", "headline": f"產生失敗：{e}"},
            "twii": None, "twoii": None, "tw": None, "us": None,
            "extras": {}, "gauges": {"items": [], "overall": "yellow",
                                     "overall_zh": "資料暫缺"},
            "checklist": {"pre": [], "intraday": [], "post": []},
        }

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    html = render_html(payload)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    kb = os.path.getsize(args.out) / 1024
    print(f"已產生 {args.out}（{kb:.0f} KB・台股 {payload.get('tw_date') or '—'}・"
          f"美股 {payload.get('us_date') or '—'}{'・DEMO' if args.demo else ''}）")


if __name__ == "__main__":
    main()
