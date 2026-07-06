# -*- coding: utf-8 -*-
r"""
輝哥選股（tw_hui_picks.py）
================================================================
「以最近一個交易日」為準的量化濾網，讀共用的 twstock.db（純價量，不需 FinMind token），
輸出 site/data/hui.json 與『單一自包含 HTML』site/hui.html。個股列表依「處置股專區」相同的
概念股分類方式分群排列（概念群在前、無概念退回產業別、未分類最後），並附一頁「公式說明」。

共 4 個濾網 ＝ 兩種型態 × 兩個版本：
  ┌ 均線突破四海遊龍（精簡版，使用者定義）
  │   1. 收盤站上 5/10/20 三線　2. 當日量 ≥ 前一日 ×1.5　3. 5/10/20 均線糾結滿 10 個交易日
  ├ 均線突破四海遊龍 AI建議版（研究強化版）
  │   4 條均線(5/10/20/60)＋時間濾網＋突破最上層均線＋ATR 強勢紅K/跳空＋量以20日均量為基準
  ├ 盤整突破（精簡版，使用者定義）
  │   1. 前 20 日區間盤整　2. 當日帶量（≥前一日 ×1.5）突破 20 日箱頂
  └ 盤整突破 AI建議版（研究強化版）
      唐奇安箱體(取昨日箱頂)＋時間濾網＋ATR 強勢收高(收盤位置)＋量以20日均量為基準

公式整理自具公信力之台股量化資料（鉅亨、MoneyDJ、財訊、永豐豐雲學堂、玉山證券、XQ、
QuantPass 等）之「均線糾結突破」「箱型／唐奇安盤整突破」通則。門檻皆環境變數可調；本頁僅供研究，非投資建議。

用法：
  python tw_hui_picks.py            # 正常（需 twstock.db）
  python tw_hui_picks.py --demo     # 離線示範（合成假資料，驗證輸出/前端）
"""
import os
import sys
import json
import math
import sqlite3
import argparse
import datetime as dt

try:
    import tw_industry
except Exception:
    tw_industry = None
try:
    import tw_concepts
except Exception:
    tw_concepts = None

DB_PATH = "twstock.db"
OUT_DIR = "site"
OUT_NAME = "hui.html"

# ---- AI建議版門檻 ----
MA_SET = (5, 10, 20, 60)
MA_SPREAD_PCT = float(os.environ.get("HUI_MA_SPREAD", "0.025") or "0.025")
MA_CONV_LOOKBACK = int(os.environ.get("HUI_MA_CONV_LOOKBACK", "10") or "10")
MA_CONV_MIN = int(os.environ.get("HUI_MA_CONV_MIN", "8") or "8")
BOX_LOOKBACK = int(os.environ.get("HUI_BOX_DAYS", "20") or "20")
BOX_WIDTH_PCT = float(os.environ.get("HUI_BOX_WIDTH", "0.06") or "0.06")
BOX_CONSOLI_BARS = int(os.environ.get("HUI_BOX_CONSOLI_BARS", "15") or "15")
BOX_CONSOLI_MIN = int(os.environ.get("HUI_BOX_CONSOLI_MIN", "13") or "13")
VOL_MULT = float(os.environ.get("HUI_VOL_MULT", "1.5") or "1.5")               # AI版：今量 ≥ 20日均量 ×1.5
VOL_AVG_DAYS = int(os.environ.get("HUI_VOL_AVG_DAYS", "20") or "20")
ATR_PERIOD = int(os.environ.get("HUI_ATR_PERIOD", "14") or "14")
ATR_BODY_MULT = float(os.environ.get("HUI_ATR_BODY_MULT", "1.0") or "1.0")
CLOSE_POS_MIN = float(os.environ.get("HUI_CLOSE_POS_MIN", "0.7") or "0.7")

# ---- 精簡版(使用者定義)門檻 ----
MA_SET_U = (5, 10, 20)
MA_SPREAD_U = float(os.environ.get("HUI_U_MA_SPREAD", "0.02") or "0.02")        # 三線價差 ÷ 收盤 ≤ 2%
MA_CONV_LB_U = int(os.environ.get("HUI_U_MA_CONV_LOOKBACK", "10") or "10")      # 均線糾結 10 個交易日
MA_CONV_MIN_U = int(os.environ.get("HUI_U_MA_CONV_MIN", "8") or "8")            # 近10根至少幾根糾結
BOX_LB_U = int(os.environ.get("HUI_U_BOX_DAYS", "20") or "20")                  # 20 天區間
BOX_WIDTH_U = float(os.environ.get("HUI_U_BOX_WIDTH", "0.15") or "0.15")        # 區間盤整箱幅 ≤ 15%
VOL_MULT_U = float(os.environ.get("HUI_U_VOL_MULT", "1.5") or "1.5")            # 精簡版：今量 ≥ 前一日 ×1.5

# ---- 共用流動性 ----
MIN_PRICE = float(os.environ.get("HUI_MIN_PRICE", "8") or "8")
MIN_AVG_AMT = float(os.environ.get("HUI_MIN_AVG_AMT", "20000000") or "20000000")
LOOKBACK_DAYS = 130

TPE_TZ = dt.timezone(dt.timedelta(hours=8))
WEEK_ZH = "一二三四五六日"


# ============================================================
#  小工具
# ============================================================
def now_taipei():
    n = dt.datetime.now(TPE_TZ)
    return f"{n:%Y-%m-%d %H:%M}（{WEEK_ZH[n.weekday()]}）"


def _r(x, n=2):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    v = round(f, n)
    return 0.0 if v == 0 else v


def _is_common_stock(sid):
    s = str(sid)
    return len(s) == 4 and s.isdigit() and not s.startswith("00")


def _ma_at(closes, n, end):
    if end + 1 < n:
        return None
    seg = closes[end - n + 1:end + 1]
    if len(seg) < n or any(x is None for x in seg):
        return None
    return sum(seg) / n


def _spread_at(closes, end, ma_set):
    c = closes[end]
    if c is None or c <= 0:
        return None
    vals = [_ma_at(closes, p, end) for p in ma_set]
    if any(v is None for v in vals):
        return None
    return (max(vals) - min(vals)) / c


def _atr(highs, lows, closes, n, end):
    trs = []
    for i in range(max(1, end - n + 1), end + 1):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        if h is None or l is None or pc is None:
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < max(2, n // 2):
        return None
    return sum(trs) / len(trs)


def _clamp01(x):
    return min(max(x, 0.0), 1.0)


# ============================================================
#  篩選：讀 DB → 逐檔算 4 個濾網（以最近一個交易日為準）
# ============================================================
def screen(con):
    diag = {"notes": []}
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM price ORDER BY date DESC LIMIT ?", (LOOKBACK_DAYS,)).fetchall()][::-1]
    if not dates:
        diag["notes"].append("price 無資料")
        return None, {"ma_user": [], "ma_ai": [], "box_user": [], "box_ai": []}, diag
    latest, d0 = dates[-1], dates[0]

    rows = con.execute(
        "SELECT stock_id,date,open,high,low,close,volume,amount FROM price WHERE date>=?", (d0,)).fetchall()
    ser = {}
    for sid, d, o, h, l, c, v, a in rows:
        if c is None:
            continue
        ser.setdefault(sid, []).append((d, o, h, l, c, v, a))

    out = {"ma_user": [], "ma_ai": [], "box_user": [], "box_ai": []}
    n_scan = 0
    for sid, arr in ser.items():
        if not _is_common_stock(sid):
            continue
        arr.sort(key=lambda x: x[0])
        if arr[-1][0] != latest:
            continue
        n_scan += 1
        opens = [x[1] for x in arr]; highs = [x[2] for x in arr]
        lows = [x[3] for x in arr]; closes = [x[4] for x in arr]
        vols = [x[5] for x in arr]; amts = [x[6] for x in arr]
        n = len(closes)
        if n < 2:
            continue
        i = n - 1
        close, openp = closes[i], opens[i]
        high, low, vtoday, prevc = highs[i], lows[i], vols[i], closes[i - 1]
        prevh, prevv = highs[i - 1], vols[i - 1]
        if close is None or vtoday is None:
            continue

        a20 = [a for a in amts[-20:] if a is not None]
        avg20amt = (sum(a20) / len(a20)) if a20 else 0.0
        if close < MIN_PRICE or avg20amt < MIN_AVG_AMT:
            continue

        # 量能：兩種基準（AI版=20日均量、精簡版=前一日）
        vwin = [v for v in vols[i - VOL_AVG_DAYS:i] if v is not None]
        avgvol = (sum(vwin) / len(vwin)) if vwin else 0.0
        volr_ai = (vtoday / avgvol) if avgvol > 0 else None
        volr_u = (vtoday / prevv) if (prevv and prevv > 0) else None

        chg = ((close / prevc - 1) * 100) if prevc else None
        atr = _atr(highs, lows, closes, ATR_PERIOD, i)
        body = (close - openp) if openp is not None else None
        strong_body = (body is not None and atr is not None and atr > 0 and body > ATR_BODY_MULT * atr)
        gap_up = (openp is not None and prevh is not None and openp > prevh)
        day_rng = (high - low) if (high is not None and low is not None) else None
        close_pos = ((close - low) / day_rng) if (day_rng and day_rng > 0) else None
        body_atr = (body / atr) if (body is not None and atr and atr > 0) else None

        # ---- 精簡版：均線突破四海遊龍（5/10/20）----
        if volr_u is not None and volr_u >= VOL_MULT_U and n >= MA_CONV_LB_U + max(MA_SET_U) + 1:
            m3 = {p: _ma_at(closes, p, i) for p in MA_SET_U}
            if all(v is not None for v in m3.values()):
                mx, mn = max(m3.values()), min(m3.values())
                above = all(close >= m3[p] for p in MA_SET_U)
                conv = sum(1 for end in range(i - MA_CONV_LB_U, i)
                           if (_spread_at(closes, end, MA_SET_U) or 9) <= MA_SPREAD_U)
                if above and conv >= MA_CONV_MIN_U:
                    spread_now = (mx - mn) / close
                    score = round(100 * (0.45 * _clamp01(1 - spread_now / MA_SPREAD_U)
                                         + 0.30 * (conv / MA_CONV_LB_U)
                                         + 0.25 * _clamp01((volr_u - VOL_MULT_U) / VOL_MULT_U)))
                    out["ma_user"].append({
                        "sid": sid, "close": _r(close), "chg": _r(chg), "volr": _r(volr_u),
                        "spread": _r(spread_now * 100, 2), "conv": conv, "convN": MA_CONV_LB_U,
                        "bias20": _r((close / m3[20] - 1) * 100, 1), "score": score})

        # ---- AI建議版：均線突破四海遊龍（5/10/20/60）----
        if volr_ai is not None and volr_ai >= VOL_MULT and n >= MA_CONV_LOOKBACK + max(MA_SET) + 1:
            ma_now = {p: _ma_at(closes, p, i) for p in MA_SET}
            if all(v is not None for v in ma_now.values()):
                maxma, minma = max(ma_now.values()), min(ma_now.values())
                spread_now = (maxma - minma) / close
                conv = sum(1 for end in range(i - MA_CONV_LOOKBACK, i)
                           if (_spread_at(closes, end, MA_SET) or 9) <= MA_SPREAD_PCT)
                if close > maxma and conv >= MA_CONV_MIN and (strong_body or gap_up):
                    score = round(100 * (0.35 * _clamp01(1 - spread_now / MA_SPREAD_PCT)
                                         + 0.25 * (conv / MA_CONV_LOOKBACK)
                                         + 0.25 * _clamp01((volr_ai - VOL_MULT) / VOL_MULT)
                                         + 0.15 * _clamp01((body_atr or 0) / 2.0)))
                    out["ma_ai"].append({
                        "sid": sid, "close": _r(close), "chg": _r(chg), "volr": _r(volr_ai),
                        "spread": _r(spread_now * 100, 2), "conv": conv, "convN": MA_CONV_LOOKBACK,
                        "bias60": _r((close / ma_now[60] - 1) * 100, 1),
                        "bodyatr": _r(body_atr, 2), "gap": bool(gap_up), "score": score})

        # ---- 精簡版：盤整突破（20 日區間突破）----
        if volr_u is not None and volr_u >= VOL_MULT_U and n >= BOX_LB_U + 1:
            bh = [h for h in highs[i - BOX_LB_U:i] if h is not None]
            bl = [l for l in lows[i - BOX_LB_U:i] if l is not None]
            if bh and bl:
                box_high, box_low = max(bh), min(bl)
                if box_high > box_low > 0:
                    rng = (box_high - box_low) / close
                    if rng <= BOX_WIDTH_U and close > box_high:
                        brk = (close / box_high - 1) * 100
                        score = round(100 * (0.45 * _clamp01(1 - rng / BOX_WIDTH_U)
                                             + 0.30 * _clamp01((volr_u - VOL_MULT_U) / VOL_MULT_U)
                                             + 0.25 * _clamp01(brk / 5.0)))
                        out["box_user"].append({
                            "sid": sid, "close": _r(close), "chg": _r(chg), "volr": _r(volr_u),
                            "boxHigh": _r(box_high), "boxLow": _r(box_low),
                            "boxWidth": _r(rng * 100, 1), "brk": _r(brk, 1), "score": score})

        # ---- AI建議版：盤整突破（唐奇安箱體）----
        if volr_ai is not None and volr_ai >= VOL_MULT and n >= BOX_LOOKBACK + BOX_CONSOLI_BARS + 1:
            bh = [h for h in highs[i - BOX_LOOKBACK:i] if h is not None]
            bl = [l for l in lows[i - BOX_LOOKBACK:i] if l is not None]
            if bh and bl:
                box_high, box_low = max(bh), min(bl)
                if box_high > box_low > 0:
                    box_width = (box_high - box_low) / close
                    cons = 0
                    for end in range(i - BOX_CONSOLI_BARS, i):
                        hw = [h for h in highs[end - BOX_LOOKBACK + 1:end + 1] if h is not None]
                        lw = [l for l in lows[end - BOX_LOOKBACK + 1:end + 1] if l is not None]
                        ce = closes[end]
                        if hw and lw and ce and (max(hw) - min(lw)) / ce <= BOX_WIDTH_PCT:
                            cons += 1
                    ok_candle = strong_body and (close_pos is not None and close_pos >= CLOSE_POS_MIN)
                    if close > box_high and cons >= BOX_CONSOLI_MIN and ok_candle:
                        brk = (close / box_high - 1) * 100
                        score = round(100 * (0.3 * _clamp01(1 - box_width / BOX_WIDTH_PCT)
                                             + 0.2 * (cons / BOX_CONSOLI_BARS)
                                             + 0.25 * _clamp01((volr_ai - VOL_MULT) / VOL_MULT)
                                             + 0.15 * _clamp01(brk / 5.0) + 0.1 * (close_pos or 0)))
                        out["box_ai"].append({
                            "sid": sid, "close": _r(close), "chg": _r(chg), "volr": _r(volr_ai),
                            "boxHigh": _r(box_high), "boxLow": _r(box_low),
                            "boxWidth": _r(box_width * 100, 1), "brk": _r(brk, 1),
                            "cons": cons, "consN": BOX_CONSOLI_BARS,
                            "closePos": _r(close_pos, 2), "score": score})

    for k in out:
        out[k].sort(key=lambda x: -(x["score"] or 0))
    diag["notes"].append(f"篩選基準日 {latest}・掃描 {n_scan} 檔・"
                         f"均線(精簡){len(out['ma_user'])}/AI {len(out['ma_ai'])}・"
                         f"盤整(精簡){len(out['box_user'])}/AI {len(out['box_ai'])}")
    return latest, out, diag


def attach_meta(con, out):
    names = dict(con.execute("SELECT stock_id,name FROM stock"))
    mkts = dict(con.execute("SELECT stock_id,market FROM stock"))
    lab = tw_industry.label_map(con) if tw_industry else {}
    try:
        cmap = tw_concepts.concept_map() if tw_concepts else {}
    except Exception:
        cmap = {}
    for L in out.values():
        for r in L:
            sid = r["sid"]
            r["name"] = names.get(sid, sid)
            r["mkt"] = mkts.get(sid, "")
            r["ind"] = lab.get(sid, "")
            r["cpt"] = cmap.get(sid, [])


# ============================================================
#  組裝 / 輸出
# ============================================================
def build_payload(latest, out, diag):
    return {
        "gentime": now_taipei(), "today": latest,
        "params": {
            "vol_mult": VOL_MULT, "vol_avg_days": VOL_AVG_DAYS,
            "ma_spread_pct": round(MA_SPREAD_PCT * 100, 1),
            "ma_conv_lookback": MA_CONV_LOOKBACK, "ma_conv_min": MA_CONV_MIN,
            "box_days": BOX_LOOKBACK, "box_width_pct": round(BOX_WIDTH_PCT * 100, 1),
            "box_consoli_bars": BOX_CONSOLI_BARS, "box_consoli_min": BOX_CONSOLI_MIN,
            "atr_period": ATR_PERIOD, "atr_body_mult": ATR_BODY_MULT, "close_pos_min": CLOSE_POS_MIN,
            "u_ma_spread_pct": round(MA_SPREAD_U * 100, 1),
            "u_ma_conv_lookback": MA_CONV_LB_U, "u_ma_conv_min": MA_CONV_MIN_U,
            "u_box_days": BOX_LB_U, "u_box_width_pct": round(BOX_WIDTH_U * 100, 1),
            "u_vol_mult": VOL_MULT_U,
            "min_price": MIN_PRICE, "min_avg_amt": MIN_AVG_AMT,
        },
        "counts": {k: len(v) for k, v in out.items()},
        "ma_user": out["ma_user"], "ma_ai": out["ma_ai"],
        "box_user": out["box_user"], "box_ai": out["box_ai"], "diag": diag,
    }


def write_outputs(out_dir, payload):
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
    with open(os.path.join(out_dir, "data", "hui.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    build_v = "".join(ch for ch in (payload.get("gentime") or "") if ch.isdigit()) or "0"
    with open(os.path.join(out_dir, OUT_NAME), "w", encoding="utf-8") as f:
        f.write(HUI_HTML.replace("__BUILDV__", build_v))
    c = payload["counts"]
    print(f"已寫出 {out_dir}/{OUT_NAME} 與 data/hui.json（基準日 {payload.get('today')}・"
          f"均線精簡 {c['ma_user']}/AI {c['ma_ai']}・盤整精簡 {c['box_user']}/AI {c['box_ai']}）")


# ============================================================
#  示範資料
# ============================================================
def make_demo():
    def mu(sid, name, mkt, close, chg, volr, spread, conv, b20, score, ind="", cpt=None):
        return {"sid": sid, "name": name, "mkt": mkt, "close": close, "chg": chg, "volr": volr,
                "spread": spread, "conv": conv, "convN": MA_CONV_LB_U, "bias20": b20,
                "score": score, "ind": ind, "cpt": cpt or []}

    def mai(sid, name, mkt, close, chg, volr, spread, conv, b60, score, ind="", cpt=None):
        return {"sid": sid, "name": name, "mkt": mkt, "close": close, "chg": chg, "volr": volr,
                "spread": spread, "conv": conv, "convN": MA_CONV_LOOKBACK, "bias60": b60,
                "bodyatr": 1.8, "gap": False, "score": score, "ind": ind, "cpt": cpt or []}

    def bu(sid, name, mkt, close, chg, volr, bh, bl, brk, bw, score, ind="", cpt=None):
        return {"sid": sid, "name": name, "mkt": mkt, "close": close, "chg": chg, "volr": volr,
                "boxHigh": bh, "boxLow": bl, "brk": brk, "boxWidth": bw, "score": score,
                "ind": ind, "cpt": cpt or []}

    def bai(sid, name, mkt, close, chg, volr, bh, bl, brk, bw, cons, score, ind="", cpt=None):
        return {"sid": sid, "name": name, "mkt": mkt, "close": close, "chg": chg, "volr": volr,
                "boxHigh": bh, "boxLow": bl, "brk": brk, "boxWidth": bw, "cons": cons,
                "consN": BOX_CONSOLI_BARS, "closePos": 0.9, "score": score, "ind": ind, "cpt": cpt or []}

    out = {
        "ma_user": [
            mu("3037", "欣興", "上市", 205.0, 6.2, 1.9, 1.4, 10, 3.1, 90, cpt=["ABF載板", "PCB"]),
            mu("8046", "南電", "上市", 158.0, 5.1, 1.7, 1.7, 9, 2.7, 82, cpt=["ABF載板"]),
            mu("2603", "長榮", "上市", 235.0, 4.4, 2.1, 1.8, 8, 2.5, 76, cpt=["航運(貨櫃/散裝)"]),
            mu("2049", "上銀", "上市", 245.0, 3.6, 1.6, 1.9, 9, 2.2, 66, cpt=["工具機", "機器人/自動化"]),
            mu("1234", "黑松", "上市", 43.2, 3.1, 1.6, 1.8, 8, 2.0, 55, ind="食品"),
        ],
        "ma_ai": [
            mai("3037", "欣興", "上市", 205.0, 6.2, 2.4, 1.6, 10, 4.5, 92, cpt=["ABF載板", "PCB"]),
            mai("2330", "台積電", "上市", 940.0, 3.4, 1.9, 2.2, 9, 3.0, 78, cpt=["CoWoS/先進封裝"]),
            mai("1519", "華城", "上市", 620.0, 5.6, 2.6, 1.8, 10, 5.1, 88, cpt=["重電"]),
        ],
        "box_user": [
            bu("3231", "緯創", "上市", 128.5, 4.8, 2.0, 122.0, 108.0, 5.3, 10.9, 84, cpt=["AI伺服器"]),
            bu("2382", "廣達", "上市", 305.0, 3.9, 1.8, 296.0, 268.0, 3.0, 9.2, 76, cpt=["AI伺服器"]),
            bu("6533", "晶心科", "上櫃", 512.0, 6.1, 2.2, 486.0, 452.0, 5.3, 6.6, 80, cpt=["IP矽智財", "IC設計"]),
            bu("9917", "中保科", "上市", 118.0, 3.4, 1.7, 114.0, 104.0, 3.5, 8.5, 58, ind="其他"),
        ],
        "box_ai": [
            bai("3231", "緯創", "上市", 128.5, 4.8, 2.3, 122.0, 116.0, 5.3, 4.9, 15, 90, cpt=["AI伺服器"]),
            bai("6533", "晶心科", "上櫃", 512.0, 6.1, 2.5, 486.0, 462.0, 5.3, 5.0, 15, 86, cpt=["IP矽智財", "IC設計"]),
        ],
    }
    diag = {"notes": ["[示範模式] 合成資料，非真實行情"]}
    return build_payload("2026-07-03", out, diag)


# ============================================================
#  主程式
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()

    if args.demo:
        write_outputs(args.out, make_demo()); return
    if not os.path.exists(DB_PATH):
        print(f"找不到 {DB_PATH}，改寫示範資料。")
        write_outputs(args.out, make_demo()); return

    con = sqlite3.connect(DB_PATH)
    try:
        latest, out, diag = screen(con)
        if latest is None:
            print("price 無資料，改寫示範資料。")
            write_outputs(args.out, make_demo()); return
        attach_meta(con, out)
    finally:
        con.close()
    write_outputs(args.out, build_payload(latest, out, diag))


# ============================================================
#  前端（自包含 HTML；資料由 data/hui.json 載入）
# ============================================================
HUI_HTML = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#000000">
<title>輝哥選股 ・ 台股看板</title>
<link rel="manifest" href="manifest.json">
<style>
  :root{
    --bg:#000000; --card:#121214; --card2:#1b1b1f; --border:#2a2a2f;
    --text:#f0f1f3; --muted:#9a9aa2; --dim:#67676e;
    --amber:#ffcf3a; --amber-s:rgba(255,207,58,.15);
    --up:#fb3b41; --down:#1ec77a;
    --blue:#5aa9ff; --blue-s:rgba(90,169,255,.12);
    --purple:#b794ff;
  }
  *{box-sizing:border-box;}
  body{margin:0; background:var(--bg); color:var(--text);
    font-family:system-ui,-apple-system,"PingFang TC","Noto Sans TC","Microsoft JhengHei","Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased; padding:16px 12px 40px; padding-top:calc(16px + env(safe-area-inset-top));}
  .num{font-variant-numeric:tabular-nums;}
  .wrap{max-width:1180px; margin:0 auto;}
  a{color:var(--blue); text-decoration:none;}
  header h1{font-size:19px; font-weight:800; margin:0;}
  header h1 .dragon{filter:saturate(1.3);}
  .sub{font-size:12px; color:var(--muted); margin-top:5px; line-height:1.6;}
  .hidden{display:none !important;}

  .cztabs{display:flex; gap:7px; margin:13px 0; padding:2px 0; border-bottom:1px solid var(--border);
    overflow-x:auto; -webkit-overflow-scrolling:touch; scrollbar-width:none;}
  .cztabs::-webkit-scrollbar{display:none;}
  .czt{flex:0 0 auto; background:transparent; color:var(--muted); border:none; border-radius:99px;
    padding:8px 14px; font-size:13px; font-weight:700; cursor:pointer; white-space:nowrap;}
  .czt.on{background:var(--amber); color:#000;}
  .czt .ai{font-size:9px; font-weight:800; background:rgba(90,169,255,.2); color:#8fd0ff; border-radius:4px; padding:0 4px; margin-left:4px; vertical-align:middle;}
  .czt.on .ai{background:rgba(0,0,0,.18); color:#0a3d78;}
  .pane{animation:fade .2s ease;}
  @keyframes fade{from{opacity:0; transform:translateY(4px);}to{opacity:1; transform:none;}}

  .intro{font-size:12.5px; color:var(--muted); line-height:1.6; background:var(--card);
    border:1px solid var(--border); border-radius:11px; padding:12px 14px; margin-bottom:12px;}
  .intro b{color:var(--text);} .intro .k{color:var(--amber); font-weight:700;}
  .intro .tagai{display:inline-block; font-size:10px; font-weight:800; background:var(--blue-s); color:#8fd0ff; border:1px solid rgba(90,169,255,.3); border-radius:5px; padding:0 6px; margin-left:4px;}
  .intro .tagu{display:inline-block; font-size:10px; font-weight:800; background:rgba(94,111,134,.18); color:#9fb0c4; border:1px solid rgba(94,111,134,.3); border-radius:5px; padding:0 6px; margin-left:4px;}
  .cnt{font-size:12px; color:var(--dim); margin:2px 2px 9px;}
  .cnt b{color:var(--amber); font-size:14px;}

  .dtbl-wrap{overflow:auto; max-height:76vh; -webkit-overflow-scrolling:touch; border:1px solid var(--border);
    border-radius:11px; background:var(--card); overscroll-behavior:contain;}
  .dtbl{border-collapse:separate; border-spacing:0; width:max-content; min-width:100%; font-variant-numeric:tabular-nums;}
  .dtbl th,.dtbl td{padding:8px 12px; text-align:right; white-space:nowrap; border-bottom:1px solid var(--border);}
  .dtbl tbody tr:last-child td{border-bottom:none;}
  .dtbl thead th{position:sticky; top:0; z-index:3; background:var(--card2); color:var(--muted); font-size:11px; font-weight:700; line-height:1.5;}
  .dtbl th.frz,.dtbl td.frz{position:sticky; left:0; z-index:2; text-align:left; background:var(--card);}
  .dtbl thead th.frz{z-index:4; background:var(--card2); box-shadow:1px 0 0 var(--border);}
  .dtbl td.frz{box-shadow:1px 0 0 var(--border);}
  .dtbl .sortlbl{cursor:pointer; display:inline-block; padding:1px 4px; border-radius:5px;}
  .dtbl .sortlbl.on{color:var(--amber); background:var(--amber-s);}
  .dtbl .sortlbl i{font-style:normal; font-size:9px; margin-left:1px;}
  .dtbl tbody tr{cursor:pointer;}
  .dtbl tbody tr:active{background:rgba(255,255,255,.05);}
  .dtbl tbody tr:active td.frz{background:#10192b;}
  .dtbl .nmcell{min-width:120px;}
  .dtbl .nmcell .nm{font-weight:700; font-size:14px; color:var(--text);}
  .dtbl .nmcell .sub{font-size:10.5px; color:var(--dim); margin-top:1px;}
  .dtbl .nmcell .cind{font-size:10px; color:#7c8aa0; font-weight:600; margin-top:1px;}
  .dtbl .cv{font-weight:800; font-size:13px;}
  .dtbl .mkt{font-size:10px; color:var(--dim); border:1px solid var(--border); border-radius:5px; padding:0 5px; margin-left:5px;}
  .gtog{background:var(--card); color:var(--muted); border:1px solid var(--border); border-radius:8px; padding:4px 10px; font-size:12px; cursor:pointer; font-weight:700; vertical-align:middle;}
  .gtog.on{background:var(--amber-s); color:var(--amber); border-color:rgba(245,165,36,.4);}
  .dtbl tr.grouphdr td{text-align:left; background:var(--card2); border-top:2px solid var(--border); padding:6px 11px; font-weight:800; font-size:13px; color:var(--text);}
  .dtbl tr.grouphdr .ghlbl{position:sticky; left:10px; display:inline-block;}
  .dtbl tr.grouphdr .gchip{display:inline-block; font-size:10px; font-weight:700; padding:1px 6px; border-radius:5px; margin-right:7px;}
  .dtbl tr.grouphdr.gc .gchip{background:rgba(77,159,255,.16); color:#6fb0ff;}
  .dtbl tr.grouphdr.gi .gchip{background:rgba(94,111,134,.18); color:#93a3b8;}
  .dtbl tr.grouphdr .gcount{color:var(--dim); font-weight:600; font-size:11px; margin-left:6px;}
  .up{color:var(--up);} .down{color:var(--down);} .flat{color:var(--muted);} .dim{color:var(--dim);}
  .amb{color:var(--amber);}
  .empty{color:var(--dim); font-size:13.5px; text-align:center; padding:36px 12px; line-height:1.7;}

  .doc{background:var(--card); border:1px solid var(--border); border-radius:11px; padding:17px 18px; line-height:1.75; font-size:14px;}
  .doc h3{font-size:15.5px; margin:22px 0 9px; color:var(--amber);}
  .doc h3:first-child{margin-top:2px;}
  .doc h4{font-size:13.5px; margin:15px 0 5px; color:var(--blue);}
  .doc p{margin:8px 0; color:var(--text);}
  .doc .lead{color:var(--muted); font-size:13.5px;}
  .doc ul{margin:8px 0; padding-left:20px;} .doc li{margin:6px 0;}
  .doc .k{color:var(--amber); font-weight:700;}
  .doc .fml{background:var(--card2); border:1px solid var(--border); border-radius:9px; padding:11px 13px; margin:10px 0; font-size:12.5px; color:var(--muted); line-height:1.7; overflow-x:auto;}
  .doc .fml b{color:var(--text);} .doc .fml code{color:#8fd0ff; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}
  .doc .warn{background:rgba(251,59,65,.14); border:1px solid rgba(255,77,79,.3); border-radius:9px; padding:11px 13px; margin:12px 0; font-size:13px; color:#ffd9da;}
  .doc table{width:100%; border-collapse:collapse; margin:12px 0; font-size:12.5px;}
  .doc th,.doc td{border:1px solid var(--border); padding:8px 9px; text-align:left; vertical-align:top;}
  .doc th{background:var(--card2); color:var(--muted); font-weight:700;}
  .doc .badge{display:inline-block; font-size:10px; font-weight:800; border-radius:5px; padding:1px 6px; margin-right:5px;}
  .doc .badge.u{background:rgba(94,111,134,.18); color:#9fb0c4;} .doc .badge.ai{background:var(--blue-s); color:#8fd0ff;}
  .discl{font-size:11.5px; color:var(--dim); margin-top:16px; line-height:1.6;}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1><span class="dragon">🐉</span> 輝哥選股</h1>
    <div class="sub">資料日 <span id="today" class="num">—</span> ・ 更新 <span id="gentime" class="num">—</span>（台北）　以<b style="color:var(--amber)">最近一個交易日</b>篩選　<button class="gtog on" id="huiGtog" title="依概念股/產業族群分組排列">☰ 依概念分群</button><br>
    <a href="index.html">← 回主看板</a>　<a href="chuzhi.html">處置股</a>　<a href="market.html">市場分析</a></div>
  </header>

  <div class="cztabs" id="cztabs">
    <button class="czt on" data-p="ma_user">均線突破四海遊龍</button>
    <button class="czt" data-p="ma_ai">均線突破四海遊龍<span class="ai">AI建議版</span></button>
    <button class="czt" data-p="box_user">盤整突破</button>
    <button class="czt" data-p="box_ai">盤整突破<span class="ai">AI建議版</span></button>
    <button class="czt" data-p="doc">公式說明</button>
  </div>

  <div class="pane" id="p-ma_user">
    <div class="intro"><b>均線突破四海遊龍</b><span class="tagu">精簡版</span>：收盤<span class="k">站上 5/10/20 三線</span>、當日量 <span class="k">≥ 前一日 ×<span id="uvm1">1.5</span></span>、且 <span class="k">5/10/20 均線糾結滿 <span id="umlb1">10</span> 個交易日</span>（近 <span id="umlb2">10</span> 根多數三線價差 ≤ <span id="usp1">2</span>%）。</div>
    <div class="cnt">符合 <b id="cnt-ma_user">—</b> 檔（依概念族群分群；點列看 K 線・點欄位標題排序）</div>
    <div id="list-ma_user"></div>
  </div>
  <div class="pane hidden" id="p-ma_ai">
    <div class="intro"><b>均線突破四海遊龍</b><span class="tagai">AI建議版</span>：在精簡版上加嚴——<span class="k">4 條均線(含季線60)</span>、突破<span class="k">最上層均線</span>、<span class="k">ATR 強勢紅K或跳空</span>、量以 <span id="vad1">20</span> 日均量為基準。糾結度 ≤ <span id="asp1">2.5</span>%、近 <span id="amlb1">10</span> 根至少 <span id="amin1">8</span> 根。</div>
    <div class="cnt">符合 <b id="cnt-ma_ai">—</b> 檔（依概念族群分群；點列看 K 線・點欄位標題排序）</div>
    <div id="list-ma_ai"></div>
  </div>
  <div class="pane hidden" id="p-box_user">
    <div class="intro"><b>盤整突破</b><span class="tagu">精簡版</span>：前 <span id="ubd1">20</span> 日<span class="k">區間盤整</span>（箱幅 ≤ <span id="ubw1">15</span>%），當日<span class="k">帶量（≥ 前一日 ×<span id="uvm2">1.5</span>）突破 <span id="ubd2">20</span> 日箱頂</span>。</div>
    <div class="cnt">符合 <b id="cnt-box_user">—</b> 檔（依概念族群分群；點列看 K 線・點欄位標題排序）</div>
    <div id="list-box_user"></div>
  </div>
  <div class="pane hidden" id="p-box_ai">
    <div class="intro"><b>盤整突破</b><span class="tagai">AI建議版</span>：唐奇安箱體（取<span class="k">昨日箱頂</span>）＋<span class="k">近 <span id="bcb1">15</span> 根多數箱幅 ≤ <span id="bw1">6</span>%</span>時間濾網＋<span class="k">ATR 強勢收高</span>（收盤位置 ≥ <span id="cp1">0.7</span>）＋量以 20 日均量為基準。</div>
    <div class="cnt">符合 <b id="cnt-box_ai">—</b> 檔（依概念族群分群；點列看 K 線・點欄位標題排序）</div>
    <div id="list-box_ai"></div>
  </div>
  <div class="pane hidden" id="p-doc"><div class="doc" id="docbody"></div></div>
</div>
<footer style="text-align:center; color:var(--dim); font-size:11px; padding:16px 14px 30px; border-top:1px solid var(--border); line-height:1.6">資料來源：<a href="https://finmindtrade.com" target="_blank" rel="noopener" style="color:var(--blue); text-decoration:none">FinMind</a>（價量）、台灣證交所／櫃買中心 ・ 篩選為本站機械式初篩・僅供研究，非投資建議</footer>

<script>
const BUILD_V = "__BUILDV__" || "0";
const $ = id => document.getElementById(id);
const esc = s => String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));
const isNum = v => v!=null && v!=="" && !isNaN(v);
function price(v){ return isNum(v)?Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}):"—"; }
function signCls(v){ return !isNum(v)?"flat":(Number(v)>0?"up":(Number(v)<0?"down":"flat")); }
function pctSigned(v,d){ d=d==null?1:d; if(!isNum(v)) return '<span class="flat">—</span>';
  const n=Number(v); return `<span class="${signCls(n)}">${n>0?"+":""}${n.toFixed(d)}%</span>`; }

let huiGroup = true;
const TABS = ["ma_user","ma_ai","box_user","box_ai"];
function groupBy(rows){
  const gm={};
  rows.forEach(r=>{
    const cs=(r.cpt&&r.cpt.length)?r.cpt:null;
    let name,isC; if(cs){ name=cs[0]; isC=true; } else { name=r.ind||"未分類"; isC=false; }
    (gm[name]||(gm[name]={name,isConcept:isC,rows:[]})).rows.push(r);
  });
  const arr=Object.keys(gm).map(k=>gm[k]);
  arr.sort((a,b)=>{ const ap=a.name==="未分類"?2:(a.isConcept?0:1), bp=b.name==="未分類"?2:(b.isConcept?0:1);
    if(ap!==bp)return ap-bp; if(b.rows.length!==a.rows.length)return b.rows.length-a.rows.length; return a.name.localeCompare(b.name); });
  return arr;
}
function groupHdr(g,cols){ return `<tr class="grouphdr ${g.isConcept?'gc':'gi'}"><td colspan="${cols}"><span class="ghlbl"><span class="gchip">${g.isConcept?'概念':'產業'}</span>${esc(g.name)}<span class="gcount">${g.rows.length}檔</span></span></td></tr>`; }

const FMT = {
  price:  v=>`<span class="cv">${price(v)}</span>`,
  pct:    v=>pctSigned(v,2),
  pct1:   v=>pctSigned(v,1),
  volr:   v=>isNum(v)?`<span class="cv amb">${Number(v).toFixed(2)}x</span>`:"—",
  sqz:    v=>isNum(v)?`<span class="cv">${Number(v).toFixed(2)}%</span>`:"—",
  plain:  v=>isNum(v)?`<span class="cv">${price(v)}</span>`:"—",
  brk:    v=>isNum(v)?`<span class="cv up">+${Number(v).toFixed(1)}%</span>`:"—",
  rng:    v=>isNum(v)?`<span class="cv">${Number(v).toFixed(1)}%</span>`:"—",
  cnt:    v=>isNum(v)?`<span class="cv">${Math.round(v)}</span>`:"—",
  score:  v=>isNum(v)?`<span class="cv amb">${Math.round(v)}</span>`:"—",
};
const COLS = {
  ma_user: [["收盤","close",FMT.price],["漲跌%","chg",FMT.pct],["量比","volr",FMT.volr],
            ["糾結度%","spread",FMT.sqz],["糾結K","conv",FMT.cnt],["距20MA%","bias20",FMT.pct1],["分數","score",FMT.score]],
  ma_ai:   [["收盤","close",FMT.price],["漲跌%","chg",FMT.pct],["量倍","volr",FMT.volr],
            ["糾結度%","spread",FMT.sqz],["糾結K","conv",FMT.cnt],["距季線%","bias60",FMT.pct1],["分數","score",FMT.score]],
  box_user:[["收盤","close",FMT.price],["漲跌%","chg",FMT.pct],["量比","volr",FMT.volr],
            ["箱頂","boxHigh",FMT.plain],["突破幅%","brk",FMT.brk],["箱幅%","boxWidth",FMT.rng],["分數","score",FMT.score]],
  box_ai:  [["收盤","close",FMT.price],["漲跌%","chg",FMT.pct],["量倍","volr",FMT.volr],
            ["箱頂","boxHigh",FMT.plain],["突破幅%","brk",FMT.brk],["箱幅%","boxWidth",FMT.rng],["盤整K","cons",FMT.cnt],["分數","score",FMT.score]],
};
const LABEL = {ma_user:"均線突破四海遊龍（精簡版）", ma_ai:"均線突破四海遊龍 AI建議版",
               box_user:"盤整突破（精簡版）", box_ai:"盤整突破 AI建議版"};
const DATA = {ma_user:[], ma_ai:[], box_user:[], box_ai:[]};
const sortState = {ma_user:{key:"score",asc:false}, ma_ai:{key:"score",asc:false},
                   box_user:{key:"score",asc:false}, box_ai:{key:"score",asc:false}};

function sortRows(name){
  const s=sortState[name], arr=(DATA[name]||[]).slice();
  if(s.key){ arr.sort((a,b)=>{ const av=a[s.key],bv=b[s.key],an=isNum(av),bn=isNum(bv);
    if(!an&&!bn)return 0; if(!an)return 1; if(!bn)return -1; return s.asc?(av-bv):(bv-av); }); }
  return arr;
}
function rowHtml(r,cols){
  let tds=""; cols.forEach(([lab,key,fmt])=>{ tds+="<td>"+fmt(r[key])+"</td>"; });
  return `<tr onclick="goChart('${esc(r.sid)}')">
    <td class="frz nmcell">
      <div class="nm">${esc(r.name||"")}${r.mkt?`<span class="mkt">${esc(r.mkt)}</span>`:""}</div>
      <div class="sub">${esc(r.sid)}</div>
      ${r.ind?`<div class="cind">${esc(r.ind)}</div>`:""}
    </td>${tds}</tr>`;
}
function renderTbl(name){
  const el=$("list-"+name); if(!el) return;
  const cols=COLS[name], data=DATA[name]||[];
  if(!data.length){ el.innerHTML=`<div class="empty">最近一個交易日沒有符合「${LABEL[name]}」條件的個股。<br><span class="dim">屬「整理→帶量→突破」型濾網，清淡或多數股尚未突破時掛零屬正常；換交易日再看。</span></div>`; return; }
  const s=sortState[name], rows=sortRows(name);
  const arrow=k=> s.key===k?(s.asc?"▲":"▼"):"";
  let thead=`<th class="frz">名稱<br><span class="sub" style="color:var(--dim)">代號 / 產業</span></th>`;
  cols.forEach(([lab,key])=>{ thead+=`<th><span class="sortlbl${s.key===key?' on':''}" data-n="${name}" data-k="${key}">${lab}<i>${arrow(key)}</i></span></th>`; });
  const ncol=cols.length+1;
  let tb="";
  if(huiGroup){ groupBy(rows).forEach(g=>{ tb+=groupHdr(g,ncol)+g.rows.map(r=>rowHtml(r,cols)).join(""); }); }
  else { tb=rows.map(r=>rowHtml(r,cols)).join(""); }
  el.innerHTML=`<div class="dtbl-wrap"><table class="dtbl"><thead><tr>${thead}</tr></thead><tbody>${tb}</tbody></table></div>`;
  el.querySelectorAll(".sortlbl").forEach(b=>b.addEventListener("click",ev=>{ ev.stopPropagation();
    const nm=b.dataset.n,k=b.dataset.k,st=sortState[nm]; if(st.key===k)st.asc=!st.asc; else{st.key=k;st.asc=false;} renderTbl(nm); }));
}

function goChart(sid){ location.href = "index.html?stk=" + encodeURIComponent(sid); }

function switchTab(p){
  document.querySelectorAll(".czt").forEach(b=>b.classList.toggle("on", b.dataset.p===p));
  TABS.concat(["doc"]).forEach(x=>{ const n=$("p-"+x); if(n) n.classList.toggle("hidden", x!==p); });
  try{ window.scrollTo({top:0,behavior:"smooth"}); }catch(e){ window.scrollTo(0,0); }
}
document.querySelectorAll(".czt").forEach(b=>b.addEventListener("click",()=>switchTab(b.dataset.p)));
$("huiGtog").addEventListener("click",e=>{ huiGroup=!huiGroup; e.currentTarget.classList.toggle("on",huiGroup); TABS.forEach(renderTbl); });

function docHtml(p){
  p=p||{};
  const uvm=p.u_vol_mult||1.5, usp=p.u_ma_spread_pct||2, umlb=p.u_ma_conv_lookback||10, umin=p.u_ma_conv_min||8,
        ubd=p.u_box_days||20, ubw=p.u_box_width_pct||15,
        vm=p.vol_mult||1.5, va=p.vol_avg_days||20, sq=p.ma_spread_pct||2.5, mlb=p.ma_conv_lookback||10, mmin=p.ma_conv_min||8,
        bd=p.box_days||20, bw=p.box_width_pct||6, bcb=p.box_consoli_bars||15, bmin=p.box_consoli_min||13,
        ap=p.atr_period||14, am=p.atr_body_mult||1.0, cp=p.close_pos_min||0.7;
  return `
  <h3>四個濾網：兩種型態 × 兩個版本</h3>
  <p class="lead">兩種都是「<b style="color:var(--text)">先沉澱、再帶量突破</b>」的起漲型態。每種型態各做兩個版本，方便你對照：<span class="badge u">精簡版</span>照你給的定義、條件較單純、選出檔數較多；<span class="badge ai">AI建議版</span>是我依專業量化研究加嚴的版本（多了時間濾網、K棒強度、以20日均量為量能基準），選出檔數較少但更嚴謹。兩者<b>用同一份最近交易日資料、同樣依概念族群分群顯示</b>。</p>

  <h3>① 均線突破四海遊龍</h3>
  <p class="lead">「四海遊龍」＝均線先「糾結」黏在一起（籌碼沉澱），再一根長紅帶量衝上均線、飛龍在天。</p>
  <h4><span class="badge u">精簡版</span>條件（同時成立）</h4>
  <div class="fml">1. 收盤<b>站上 5/10/20 三線</b>：<code>Close ≥ MA5, MA10, MA20</code><br>
    2. <b>帶量</b>：<code>今量 ≥ 前一日量 × ${uvm}</code><br>
    3. <b>均線糾結 ${umlb} 個交易日</b>：近 ${umlb} 根 K 棒至少 ${umin} 根三線價差 <code>(max−min of MA5,10,20)/Close ≤ ${usp}%</code></div>
  <h4><span class="badge ai">AI建議版</span>加嚴之處</h4>
  <div class="fml">・均線改 <b>4 條(5/10/20/60)</b>、糾結度 ≤ ${sq}%、近 ${mlb} 根至少 ${mmin} 根<br>
    ・突破要<b>站上並突破最上層均線</b> <code>Close &gt; max(MA5,10,20,60)</code><br>
    ・加 <b>K棒強度</b>：實體紅K <code>Close−Open &gt; ATR(${ap})×${am}</code> 或跳空（開&gt;昨高）<br>
    ・量能改以 <b>${va} 日均量</b> 為基準：<code>今量 &gt; MA(Vol,${va}) × ${vm}</code></div>

  <h3>② 盤整突破</h3>
  <p class="lead">股價在一個箱型區間橫盤，直到某天帶量向上突破箱頂。</p>
  <h4><span class="badge u">精簡版</span>條件（同時成立）</h4>
  <div class="fml">1. <b>前 ${ubd} 日區間盤整</b>：箱幅 <code>(箱頂−箱底)/Close ≤ ${ubw}%</code>（箱頂/底＝近 ${ubd} 日最高/最低）<br>
    2. <b>突破箱頂</b>：<code>Close &gt; 近${ubd}日最高</code>（創 ${ubd} 日新高）<br>
    3. <b>帶量</b>：<code>今量 ≥ 前一日量 × ${uvm}</code></div>
  <h4><span class="badge ai">AI建議版</span>加嚴之處</h4>
  <div class="fml">・箱頂取<b>昨日值</b> <code>Box_High[1]</code>（避免當日新高墊高箱頂而永遠突破不了）<br>
    ・加<b>時間濾網</b>：近 ${bcb} 根至少 ${bmin} 根箱幅 ≤ ${bw}%（確保長時間橫盤）<br>
    ・加 <b>K棒強度</b>：實體 &gt; ATR(${ap})×${am} 且 收盤位置 <code>(Close−Low)/(High−Low) ≥ ${cp}</code>（避免長上影線假突破）<br>
    ・量能改以 <b>${va} 日均量</b> 為基準</div>

  <h3>兩版本對照</h3>
  <table>
    <tr><th>項目</th><th><span class="badge u">精簡版</span></th><th><span class="badge ai">AI建議版</span></th></tr>
    <tr><td>均線條數</td><td>5/10/20（3 條）</td><td>5/10/20/60（4 條）</td></tr>
    <tr><td>糾結度門檻</td><td>≤ ${usp}%（${umin}/${umlb} 根）</td><td>≤ ${sq}%（${mmin}/${mlb} 根）</td></tr>
    <tr><td>均線突破定義</td><td>站上三線</td><td>突破最上層均線＋強勢K棒</td></tr>
    <tr><td>盤整箱幅</td><td>≤ ${ubw}%（單根）</td><td>≤ ${bw}%（${bmin}/${bcb} 根時間濾網）</td></tr>
    <tr><td>盤整突破強度</td><td>突破 ${ubd} 日高</td><td>突破昨日箱頂＋收高＋實體&gt;ATR</td></tr>
    <tr><td>量能基準</td><td>前一日量 ×${uvm}</td><td>${va} 日均量 ×${vm}</td></tr>
    <tr><td>選出檔數</td><td>較多（較寬鬆）</td><td>較少（較嚴、假突破更少）</td></tr>
  </table>

  <h3>怎麼看表</h3>
  <ul>
    <li><b>量比／量倍</b>：精簡版＝今量÷前一日；AI版＝今量÷${va}日均量。<b>糾結度%／箱幅%</b> 越小＝整理越緊；<b>糾結K／盤整K</b>＝時間濾網達標根數。</li>
    <li><b>分數</b>：綜合「整理越緊、量能越大、突破越強」的 0–100 分，只供<b>排序</b>參考，非買賣訊號。</li>
    <li>清單<b>依概念族群分群</b>；點列直接開 <b>K 線圖</b>再確認籌碼。停損可設在「帶量突破紅K的最低點」，跌破多為假突破。</li>
  </ul>
  <div class="warn"><b>假突破風險</b>：帶量突破後若隔天量縮、收盤跌回均線糾結區或箱頂之下，多半是假突破，應嚴設停損。這些是「整理→突破」型濾網，某些交易日可能掛零，屬正常。</div>
  <p class="discl">公式整理自具公信力之台股量化資料（鉅亨、MoneyDJ、財訊、永豐豐雲學堂、玉山證券、XQ、QuantPass 等）之「均線糾結突破」「箱型／唐奇安盤整突破」通則。門檻皆環境變數可調（精簡版 HUI_U_*、AI建議版 HUI_*）；本頁為研究整理，非投資建議。</p>`;
}

async function boot(){
  let d=null;
  try{ const r=await fetch("data/hui.json?v="+BUILD_V,{cache:"default"}); if(r.ok) d=await r.json(); }catch(e){}
  if(!d){
    $("today").textContent="資料尚未產生";
    TABS.forEach(k=>{ const el=$("list-"+k); if(el) el.innerHTML=`<div class="empty">尚未取得資料。<br>請先在 GitHub Actions 跑一次工作流程產生 data/hui.json。</div>`; });
    $("docbody").innerHTML=docHtml({});
    return;
  }
  $("today").textContent=d.today||"—";
  $("gentime").textContent=d.gentime||"—";
  const c=d.counts||{}, p=d.params||{};
  TABS.forEach(k=>{ const e=$("cnt-"+k); if(e) e.textContent=(c[k]!=null?c[k]:(d[k]||[]).length); DATA[k]=d[k]||[]; });
  const setT=(id,v)=>{const e=$(id); if(e&&v!=null)e.textContent=v;};
  setT("uvm1",p.u_vol_mult); setT("uvm2",p.u_vol_mult); setT("umlb1",p.u_ma_conv_lookback); setT("umlb2",p.u_ma_conv_lookback); setT("usp1",p.u_ma_spread_pct);
  setT("ubd1",p.u_box_days); setT("ubd2",p.u_box_days); setT("ubw1",p.u_box_width_pct);
  setT("vad1",p.vol_avg_days); setT("asp1",p.ma_spread_pct); setT("amlb1",p.ma_conv_lookback); setT("amin1",p.ma_conv_min);
  setT("bcb1",p.box_consoli_bars); setT("bw1",p.box_width_pct); setT("cp1",p.close_pos_min);
  TABS.forEach(renderTbl);
  $("docbody").innerHTML=docHtml(p);
}
boot();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
