# -*- coding: utf-8 -*-
r"""
輝哥選股（tw_hui_picks.py）
================================================================
兩個「以最近一個交易日」為準的量化濾網，讀共用的 twstock.db（純價量，不需 FinMind token），
輸出 site/data/hui.json 與『單一自包含 HTML』site/hui.html。個股列表依「處置股專區」相同的
概念股分類方式分群排列（概念群在前、無概念退回產業別、未分類最後），並附一頁「公式說明」。

演算法（依使用者提供之專業量化公式實作，強調「持續整理 → 帶量突破 → K棒強度」以濾假突破）：

濾網一：均線突破四海遊龍（MA5/10/20/60 糾結後帶量突破）
  1. 量化糾結（最大最小相對價差法）：
       MA_Spread = (max(MA5,MA10,MA20,MA60) − min(...)) / Close ×100%
  2. 時間濾網（避免只是均線交叉的瞬間）：近 MA_CONV_LOOKBACK 根 K 棒中，
       至少 MA_CONV_MIN 根滿足 MA_Spread ≤ MA_SPREAD_PCT。
  3. 突破訊號：收盤站上並突破最上層均線 Close > max(MA5,MA10,MA20,MA60)。
  4. K棒強度濾網（避免被巴）：實體紅K（Close−Open > ATR×ATR_BODY_MULT）或跳空（Open > 昨高）。
  5. 量能濾網：Volume > MA(Volume,20) × VOL_MULT。

濾網二：盤整突破（唐奇安箱體 + 帶量突破箱頂）
  1. 箱體量化：Box_High = Highest(High,N)[1]、Box_Low = Lowest(Low,N)[1]；
       Box_Width = (Box_High − Box_Low) / Close ×100%。
  2. 時間濾網：近 BOX_CONSOLI_BARS 根 K 棒中，至少 BOX_CONSOLI_MIN 根之 N 日 Box_Width ≤ BOX_WIDTH_PCT
       （確保長時間橫盤、而非剛從大波動平復）。
  3. 突破訊號：Close > Box_High[1]（用昨日箱頂，否則當日新高會同步墊高箱頂而永遠無法突破）。
  4. K棒強度：實體 Close−Open > ATR×ATR_BODY_MULT 且收盤位置 (Close−Low)/(High−Low) ≥ CLOSE_POS_MIN
       （收盤貼近當日高、避免長上影線假突破）。
  5. 量能濾網：Volume > MA(Volume,20) × VOL_MULT。

門檻皆以環境變數可調；公式整理自具公信力之台股量化資料（鉅亨、MoneyDJ、財訊、永豐豐雲學堂、
QuantPass 等）之「均線糾結突破」「箱型／唐奇安盤整突破」通則。本頁僅供研究，非投資建議。

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

# ---- 可調門檻（環境變數）----
MA_SET = (5, 10, 20, 60)
MA_SPREAD_PCT = float(os.environ.get("HUI_MA_SPREAD", "0.025") or "0.025")       # 四線價差 ÷ 收盤 ≤ 2.5%
MA_CONV_LOOKBACK = int(os.environ.get("HUI_MA_CONV_LOOKBACK", "10") or "10")     # 糾結時間濾網回看根數
MA_CONV_MIN = int(os.environ.get("HUI_MA_CONV_MIN", "8") or "8")                 # 至少幾根達糾結（8/10）
BOX_LOOKBACK = int(os.environ.get("HUI_BOX_DAYS", "20") or "20")                 # 唐奇安箱體週期 N
BOX_WIDTH_PCT = float(os.environ.get("HUI_BOX_WIDTH", "0.06") or "0.06")         # 箱幅 ÷ 收盤 ≤ 6%
BOX_CONSOLI_BARS = int(os.environ.get("HUI_BOX_CONSOLI_BARS", "15") or "15")     # 盤整時間濾網回看根數
BOX_CONSOLI_MIN = int(os.environ.get("HUI_BOX_CONSOLI_MIN", "13") or "13")       # 至少幾根箱幅達標（13/15）
VOL_MULT = float(os.environ.get("HUI_VOL_MULT", "1.5") or "1.5")                 # 帶量：今量 ≥ 20日均量 ×1.5
VOL_AVG_DAYS = int(os.environ.get("HUI_VOL_AVG_DAYS", "20") or "20")
ATR_PERIOD = int(os.environ.get("HUI_ATR_PERIOD", "14") or "14")
ATR_BODY_MULT = float(os.environ.get("HUI_ATR_BODY_MULT", "1.0") or "1.0")       # 突破實體 ≥ ATR ×1.0
CLOSE_POS_MIN = float(os.environ.get("HUI_CLOSE_POS_MIN", "0.7") or "0.7")       # 收盤位置（盤整用）
MIN_PRICE = float(os.environ.get("HUI_MIN_PRICE", "8") or "8")                   # 最低價（濾流動性差）
MIN_AVG_AMT = float(os.environ.get("HUI_MIN_AVG_AMT", "20000000") or "20000000")  # 20日均額 ≥ 2000萬元
LOOKBACK_DAYS = 130                                                              # 讀取交易日數（足夠 MA60＋濾網）

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
    """MA_n（收盤簡單平均），結束於索引 end（含）。資料不足或含 None 回 None。"""
    if end + 1 < n:
        return None
    seg = closes[end - n + 1:end + 1]
    if len(seg) < n or any(x is None for x in seg):
        return None
    return sum(seg) / n


def _spread_at(closes, end):
    """四線最大最小相對價差（÷該根收盤）。任一均線缺值回 None。"""
    c = closes[end]
    if c is None or c <= 0:
        return None
    vals = [_ma_at(closes, p, end) for p in MA_SET]
    if any(v is None for v in vals):
        return None
    return (max(vals) - min(vals)) / c


def _atr(highs, lows, closes, n, end):
    """ATR（近 n 根真實波幅平均），結束於索引 end。不足回 None。"""
    trs = []
    for i in range(max(1, end - n + 1), end + 1):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        if h is None or l is None or pc is None:
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < max(2, n // 2):
        return None
    return sum(trs) / len(trs)


# ============================================================
#  篩選：讀 DB → 逐檔算兩個濾網（以最近一個交易日為準）
# ============================================================
def screen(con):
    diag = {"notes": []}
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM price ORDER BY date DESC LIMIT ?", (LOOKBACK_DAYS,)).fetchall()][::-1]
    if not dates:
        diag["notes"].append("price 無資料")
        return None, [], [], diag
    latest, d0 = dates[-1], dates[0]

    rows = con.execute(
        "SELECT stock_id,date,open,high,low,close,volume,amount FROM price WHERE date>=?", (d0,)).fetchall()
    ser = {}
    for sid, d, o, h, l, c, v, a in rows:
        if c is None:
            continue
        ser.setdefault(sid, []).append((d, o, h, l, c, v, a))

    ma_list, box_list = [], []
    n_scan = 0
    for sid, arr in ser.items():
        if not _is_common_stock(sid):
            continue
        arr.sort(key=lambda x: x[0])
        if arr[-1][0] != latest:            # 最近一日沒交易 → 不列入當日篩選
            continue
        n_scan += 1
        opens = [x[1] for x in arr]; highs = [x[2] for x in arr]
        lows = [x[3] for x in arr]; closes = [x[4] for x in arr]
        vols = [x[5] for x in arr]; amts = [x[6] for x in arr]
        n = len(closes)
        if n < 2:
            continue
        i = n - 1                            # 今日索引
        close, openp = closes[i], opens[i]
        high, low, vtoday, prevc = highs[i], lows[i], vols[i], closes[i - 1]
        prevh = highs[i - 1]
        if close is None or vtoday is None:
            continue

        # 流動性 / 價格門檻
        a20 = [a for a in amts[-20:] if a is not None]
        avg20amt = (sum(a20) / len(a20)) if a20 else 0.0
        if close < MIN_PRICE or avg20amt < MIN_AVG_AMT:
            continue

        # 量能：今量 ÷ 近 VOL_AVG_DAYS 日均量（不含今日）
        vwin = [v for v in vols[i - VOL_AVG_DAYS:i] if v is not None]
        avgvol = (sum(vwin) / len(vwin)) if vwin else 0.0
        volr = (vtoday / avgvol) if avgvol > 0 else None
        if not (volr is not None and volr >= VOL_MULT):
            continue                          # 兩濾網皆要求帶量

        chg = ((close / prevc - 1) * 100) if prevc else None
        atr = _atr(highs, lows, closes, ATR_PERIOD, i)
        body = (close - openp) if openp is not None else None
        strong_body = (body is not None and atr is not None and atr > 0 and body > ATR_BODY_MULT * atr)
        gap_up = (openp is not None and prevh is not None and openp > prevh)
        day_rng = (high - low) if (high is not None and low is not None) else None
        close_pos = ((close - low) / day_rng) if (day_rng and day_rng > 0) else None
        body_atr = (body / atr) if (body is not None and atr and atr > 0) else None

        # ---- 濾網一：均線突破四海遊龍 ----
        if n >= MA_CONV_LOOKBACK + max(MA_SET) + 1:
            ma_now = {p: _ma_at(closes, p, i) for p in MA_SET}
            if all(v is not None for v in ma_now.values()):
                maxma, minma = max(ma_now.values()), min(ma_now.values())
                spread_now = (maxma - minma) / close
                conv = 0
                for end in range(i - MA_CONV_LOOKBACK, i):     # 今日之前 MA_CONV_LOOKBACK 根
                    sp = _spread_at(closes, end)
                    if sp is not None and sp <= MA_SPREAD_PCT:
                        conv += 1
                breakout = close > maxma
                candle_ok = strong_body or gap_up
                if breakout and conv >= MA_CONV_MIN and candle_ok:
                    tight = max(0.0, 1 - spread_now / MA_SPREAD_PCT)
                    convsc = conv / MA_CONV_LOOKBACK
                    volsc = min(max((volr - VOL_MULT) / VOL_MULT, 0.0), 1.0)
                    bodysc = min((body_atr or 0) / 2.0, 1.0)
                    score = round(100 * (0.35 * tight + 0.25 * convsc + 0.25 * volsc + 0.15 * bodysc))
                    ma_list.append({
                        "sid": sid, "close": _r(close), "chg": _r(chg), "volr": _r(volr),
                        "spread": _r(spread_now * 100, 2), "conv": conv, "convN": MA_CONV_LOOKBACK,
                        "bias60": _r((close / ma_now[60] - 1) * 100, 1),
                        "bodyatr": _r(body_atr, 2), "gap": bool(gap_up), "score": score})

        # ---- 濾網二：盤整突破（唐奇安箱體）----
        if n >= BOX_LOOKBACK + BOX_CONSOLI_BARS + 1:
            bh = [h for h in highs[i - BOX_LOOKBACK:i] if h is not None]   # 昨日為止的箱頂/箱底
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
                        if not hw or not lw or not ce:
                            continue
                        if (max(hw) - min(lw)) / ce <= BOX_WIDTH_PCT:
                            cons += 1
                    breakout = close > box_high
                    candle_ok = strong_body and (close_pos is not None and close_pos >= CLOSE_POS_MIN)
                    if breakout and cons >= BOX_CONSOLI_MIN and candle_ok:
                        brk = (close / box_high - 1) * 100
                        tight = max(0.0, 1 - box_width / BOX_WIDTH_PCT)
                        conssc = cons / BOX_CONSOLI_BARS
                        volsc = min(max((volr - VOL_MULT) / VOL_MULT, 0.0), 1.0)
                        brksc = min(brk / 5.0, 1.0)
                        score = round(100 * (0.3 * tight + 0.2 * conssc + 0.25 * volsc
                                             + 0.15 * brksc + 0.1 * (close_pos or 0)))
                        box_list.append({
                            "sid": sid, "close": _r(close), "chg": _r(chg), "volr": _r(volr),
                            "boxHigh": _r(box_high), "boxLow": _r(box_low),
                            "boxWidth": _r(box_width * 100, 1), "brk": _r(brk, 1),
                            "cons": cons, "consN": BOX_CONSOLI_BARS,
                            "closePos": _r(close_pos, 2), "score": score})

    ma_list.sort(key=lambda x: -(x["score"] or 0))
    box_list.sort(key=lambda x: -(x["score"] or 0))
    diag["notes"].append(f"篩選基準日 {latest}・掃描 {n_scan} 檔・"
                         f"均線突破 {len(ma_list)}・盤整突破 {len(box_list)}")
    return latest, ma_list, box_list, diag


def attach_meta(con, *lists):
    names = dict(con.execute("SELECT stock_id,name FROM stock"))
    mkts = dict(con.execute("SELECT stock_id,market FROM stock"))
    lab = tw_industry.label_map(con) if tw_industry else {}
    try:
        cmap = tw_concepts.concept_map() if tw_concepts else {}
    except Exception:
        cmap = {}
    for L in lists:
        for r in L:
            sid = r["sid"]
            r["name"] = names.get(sid, sid)
            r["mkt"] = mkts.get(sid, "")
            r["ind"] = lab.get(sid, "")
            r["cpt"] = cmap.get(sid, [])


# ============================================================
#  組裝 / 輸出
# ============================================================
def build_payload(latest, ma_list, box_list, diag):
    return {
        "gentime": now_taipei(), "today": latest,
        "params": {
            "vol_mult": VOL_MULT, "vol_avg_days": VOL_AVG_DAYS,
            "ma_spread_pct": round(MA_SPREAD_PCT * 100, 1),
            "ma_conv_lookback": MA_CONV_LOOKBACK, "ma_conv_min": MA_CONV_MIN,
            "box_days": BOX_LOOKBACK, "box_width_pct": round(BOX_WIDTH_PCT * 100, 1),
            "box_consoli_bars": BOX_CONSOLI_BARS, "box_consoli_min": BOX_CONSOLI_MIN,
            "atr_period": ATR_PERIOD, "atr_body_mult": ATR_BODY_MULT, "close_pos_min": CLOSE_POS_MIN,
            "min_price": MIN_PRICE, "min_avg_amt": MIN_AVG_AMT,
        },
        "counts": {"ma": len(ma_list), "box": len(box_list)},
        "ma": ma_list, "box": box_list, "diag": diag,
    }


def write_outputs(out_dir, payload):
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
    with open(os.path.join(out_dir, "data", "hui.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    build_v = "".join(ch for ch in (payload.get("gentime") or "") if ch.isdigit()) or "0"
    html = HUI_HTML.replace("__BUILDV__", build_v)
    with open(os.path.join(out_dir, OUT_NAME), "w", encoding="utf-8") as f:
        f.write(html)
    c = payload["counts"]
    print(f"已寫出 {out_dir}/{OUT_NAME} 與 data/hui.json"
          f"（基準日 {payload.get('today')}・均線突破 {c['ma']}・盤整突破 {c['box']}）")


# ============================================================
#  示範資料
# ============================================================
def make_demo():
    def ma(sid, name, mkt, close, chg, volr, spread, conv, b60, score, ind="", cpt=None):
        return {"sid": sid, "name": name, "mkt": mkt, "close": close, "chg": chg, "volr": volr,
                "spread": spread, "conv": conv, "convN": MA_CONV_LOOKBACK, "bias60": b60,
                "bodyatr": 1.8, "gap": False, "score": score, "ind": ind, "cpt": cpt or []}

    def bx(sid, name, mkt, close, chg, volr, bh, bl, brk, bw, cons, score, ind="", cpt=None):
        return {"sid": sid, "name": name, "mkt": mkt, "close": close, "chg": chg, "volr": volr,
                "boxHigh": bh, "boxLow": bl, "brk": brk, "boxWidth": bw, "cons": cons,
                "consN": BOX_CONSOLI_BARS, "closePos": 0.9, "score": score, "ind": ind, "cpt": cpt or []}

    ma_list = [
        ma("3037", "欣興", "上市", 205.0, 6.2, 2.4, 1.6, 10, 4.5, 92, cpt=["ABF載板", "PCB"]),
        ma("8046", "南電", "上市", 158.0, 5.1, 2.1, 2.0, 9, 3.9, 84, cpt=["ABF載板"]),
        ma("2330", "台積電", "上市", 940.0, 3.4, 1.9, 2.2, 9, 3.0, 78, cpt=["CoWoS/先進封裝"]),
        ma("1519", "華城", "上市", 620.0, 5.6, 2.6, 1.8, 10, 5.1, 88, cpt=["重電"]),
        ma("1234", "黑松", "上市", 43.2, 3.6, 1.9, 2.3, 8, 3.5, 58, ind="食品"),
    ]
    box_list = [
        bx("3231", "緯創", "上市", 128.5, 4.8, 2.3, 122.0, 116.0, 5.3, 4.9, 15, 90, cpt=["AI伺服器"]),
        bx("2382", "廣達", "上市", 305.0, 3.9, 2.0, 296.0, 282.0, 3.0, 4.7, 14, 80, cpt=["AI伺服器"]),
        bx("6533", "晶心科", "上櫃", 512.0, 6.1, 2.5, 486.0, 462.0, 5.3, 5.0, 15, 86, cpt=["IP矽智財", "IC設計"]),
        bx("2603", "長榮", "上市", 235.0, 4.4, 2.2, 224.0, 213.0, 4.9, 4.9, 13, 79, cpt=["航運(貨櫃/散裝)"]),
        bx("9917", "中保科", "上市", 118.0, 3.7, 1.7, 114.0, 108.5, 3.5, 5.0, 14, 55, ind="其他"),
    ]
    diag = {"notes": ["[示範模式] 合成資料，非真實行情"]}
    return build_payload("2026-07-03", ma_list, box_list, diag)


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
        latest, ma_list, box_list, diag = screen(con)
        if latest is None:
            print("price 無資料，改寫示範資料。")
            write_outputs(args.out, make_demo()); return
        attach_meta(con, ma_list, box_list)
    finally:
        con.close()
    write_outputs(args.out, build_payload(latest, ma_list, box_list, diag))


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

  .cztabs{display:flex; gap:8px; margin:13px 0; padding:2px 0; border-bottom:1px solid var(--border);
    overflow-x:auto; -webkit-overflow-scrolling:touch; scrollbar-width:none;}
  .cztabs::-webkit-scrollbar{display:none;}
  .czt{flex:0 0 auto; background:transparent; color:var(--muted); border:none; border-radius:99px;
    padding:8px 15px; font-size:13.5px; font-weight:700; cursor:pointer; white-space:nowrap;}
  .czt.on{background:var(--amber); color:#000;}
  .pane{animation:fade .2s ease;}
  @keyframes fade{from{opacity:0; transform:translateY(4px);}to{opacity:1; transform:none;}}

  .intro{font-size:12.5px; color:var(--muted); line-height:1.6; background:var(--card);
    border:1px solid var(--border); border-radius:11px; padding:12px 14px; margin-bottom:12px;}
  .intro b{color:var(--text);} .intro .k{color:var(--amber); font-weight:700;}
  .cnt{font-size:12px; color:var(--dim); margin:2px 2px 9px;}
  .cnt b{color:var(--amber); font-size:14px;}

  /* 緊湊表格：凍結首欄、可左右滑動、點欄位標題排序 */
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
  /* 概念分群：切換鈕 + 群組標題列（文字固定在最左） */
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
  .doc .step{display:flex; gap:11px; margin:10px 0;}
  .doc .step .no{flex:0 0 26px; height:26px; border-radius:99px; background:var(--amber-s); color:var(--amber); font-weight:800; font-size:13px; display:flex; align-items:center; justify-content:center;}
  .doc .fml{background:var(--card2); border:1px solid var(--border); border-radius:9px; padding:11px 13px; margin:10px 0; font-size:12.5px; color:var(--muted); line-height:1.7; overflow-x:auto;}
  .doc .fml b{color:var(--text);} .doc .fml code{color:#8fd0ff; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}
  .doc .warn{background:rgba(251,59,65,.14); border:1px solid rgba(255,77,79,.3); border-radius:9px; padding:11px 13px; margin:12px 0; font-size:13px; color:#ffd9da;}
  .doc table{width:100%; border-collapse:collapse; margin:12px 0; font-size:12.5px;}
  .doc th,.doc td{border:1px solid var(--border); padding:8px 9px; text-align:left; vertical-align:top;}
  .doc th{background:var(--card2); color:var(--muted); font-weight:700;}
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
    <button class="czt on" data-p="ma">均線突破四海遊龍</button>
    <button class="czt" data-p="box">盤整突破</button>
    <button class="czt" data-p="doc">公式說明</button>
  </div>

  <div class="pane" id="p-ma">
    <div class="intro"><b>均線突破四海遊龍</b>：<span class="k">5/10/20/60 日均線糾結</span>（近 <span id="malb-i">10</span> 根多數 K 棒四線價差 ≤ <span id="masp-i">2.5</span>%）後，
      收盤<span class="k">突破最上層均線</span>、<span class="k">帶量</span>（量 ≥ 20 日均量 ×<span id="vm-i">1.5</span>）且為<span class="k">強勢紅K</span>（實體 ＞ ATR 或跳空）。糾結度越小＝四線越黏。</div>
    <div class="cnt">符合 <b id="cnt-ma">—</b> 檔（依概念族群分群；點列看 K 線・點欄位標題排序）</div>
    <div id="list-ma"></div>
  </div>

  <div class="pane hidden" id="p-box">
    <div class="intro"><b>盤整突破</b>：前 <span class="k" id="boxdays-i">20</span> 日<span class="k">唐奇安箱體</span>長時間收窄
      （近 <span id="bcb-i">15</span> 根多數 K 棒箱幅 ≤ <span id="bw-i">6</span>%），今日收盤<span class="k">帶量突破昨日箱頂</span>、
      且為<span class="k">強勢紅K收高</span>（實體 ＞ ATR 且收盤貼近當日高）。箱幅％越小＝盤整越緊。</div>
    <div class="cnt">符合 <b id="cnt-box">—</b> 檔（依概念族群分群；點列看 K 線・點欄位標題排序）</div>
    <div id="list-box"></div>
  </div>

  <div class="pane hidden" id="p-doc">
    <div class="doc" id="docbody"></div>
  </div>
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

/* 概念分群：概念(cpt 主概念)優先，退回產業(ind)，再退回未分類；概念群在前、產業次之、未分類最後，同層依檔數多寡 */
let huiGroup = true;
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

/* 欄位定義：[label, key, formatter] */
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
  ma: [["收盤","close",FMT.price],["漲跌%","chg",FMT.pct],["量倍","volr",FMT.volr],
       ["糾結度%","spread",FMT.sqz],["糾結K","conv",FMT.cnt],["距季線%","bias60",FMT.pct1],["分數","score",FMT.score]],
  box:[["收盤","close",FMT.price],["漲跌%","chg",FMT.pct],["量倍","volr",FMT.volr],
       ["箱頂","boxHigh",FMT.plain],["突破幅%","brk",FMT.brk],["箱幅%","boxWidth",FMT.rng],["盤整K","cons",FMT.cnt],["分數","score",FMT.score]],
};
const DATA = {ma:[], box:[]};
const sortState = {ma:{key:"score",asc:false}, box:{key:"score",asc:false}};

function sortRows(name){
  const s=sortState[name], arr=(DATA[name]||[]).slice();
  if(s.key){ arr.sort((a,b)=>{ const av=a[s.key],bv=b[s.key],an=isNum(av),bn=isNum(bv);
    if(!an&&!bn)return 0; if(!an)return 1; if(!bn)return -1; return s.asc?(av-bv):(bv-av); }); }
  return arr;
}
function rowHtml(r,cols){
  let tds="";
  cols.forEach(([lab,key,fmt])=>{ tds+="<td>"+fmt(r[key])+"</td>"; });
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
  if(!data.length){ el.innerHTML=`<div class="empty">最近一個交易日沒有符合「${name==="ma"?"均線突破四海遊龍":"盤整突破"}」條件的個股。<br><span class="dim">這兩個是嚴格濾網（持續糾結／盤整＋帶量＋強勢突破），清淡或多數股尚未突破時掛零屬正常；換交易日再看。</span></div>`; return; }
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
    const n=b.dataset.n,k=b.dataset.k,st=sortState[n]; if(st.key===k)st.asc=!st.asc; else{st.key=k;st.asc=false;} renderTbl(n); }));
}

function goChart(sid){ location.href = "index.html?stk=" + encodeURIComponent(sid); }

function switchTab(p){
  document.querySelectorAll(".czt").forEach(b=>b.classList.toggle("on", b.dataset.p===p));
  ["ma","box","doc"].forEach(x=>{ const n=$("p-"+x); if(n) n.classList.toggle("hidden", x!==p); });
  try{ window.scrollTo({top:0,behavior:"smooth"}); }catch(e){ window.scrollTo(0,0); }
}
document.querySelectorAll(".czt").forEach(b=>b.addEventListener("click",()=>switchTab(b.dataset.p)));
$("huiGtog").addEventListener("click",e=>{ huiGroup=!huiGroup; e.currentTarget.classList.toggle("on",huiGroup); ["ma","box"].forEach(renderTbl); });

function docHtml(p){
  p=p||{};
  const vm=p.vol_mult||1.5, va=p.vol_avg_days||20, sq=p.ma_spread_pct||2.5,
        mlb=p.ma_conv_lookback||10, mmin=p.ma_conv_min||8,
        bd=p.box_days||20, bw=p.box_width_pct||6, bcb=p.box_consoli_bars||15, bmin=p.box_consoli_min||13,
        ap=p.atr_period||14, am=p.atr_body_mult||1.0, cp=p.close_pos_min||0.7;
  return `
  <h3>兩種濾網在找什麼？</h3>
  <p class="lead">兩個都是「<b style="color:var(--text)">先沉澱、再帶量突破</b>」的起漲型態：股價先進入一段「大家看法僵持、量縮盤整」的階段，把浮動籌碼洗乾淨；一旦主力進場帶量往上拉，往往就是一段行情的起點。差別只在「怎麼定義沉澱」——一個看<b>均線黏合</b>，一個看<b>價格箱體</b>。兩者都要求「<span class="k">持續整理 → 帶量突破 → 強勢K棒</span>」三者<b>同時</b>成立，用嚴格條件濾掉沒量、撐不住的假突破。</p>

  <h3>① 均線突破四海遊龍</h3>
  <p class="lead">「四海遊龍」＝ <b>5、10、20、60 四條均線</b>先「糾結」在一起（像四條龍盤在一起休息），再一根長紅同時衝上四條線、飛龍在天。</p>
  <h4>白話原理</h4>
  <p>當短中長期均線都黏在一起，代表這段期間股價幾乎沒漲沒跌、成本墊高得差不多，<b>沒信心的人慢慢退場、籌碼變乾淨</b>。此時只要有一方帶量把股價往上拉，上方套牢賣壓很少，容易一路噴出。</p>
  <h4>量化條件（需同時成立）</h4>
  <div class="step"><div class="no">1</div><div class="tx"><b>量化糾結</b>（最大最小相對價差法）：<span class="k">糾結度％ = (四線最高 − 四線最低) ÷ 收盤 ×100</span>，越小＝四線越黏。</div></div>
  <div class="step"><div class="no">2</div><div class="tx"><b>時間濾網</b>（避免只是均線交叉的瞬間）：近 <span class="k">${mlb}</span> 根 K 棒中，至少 <span class="k">${mmin}</span> 根糾結度 ≤ <span class="k">${sq}%</span>（表中「糾結K」欄）。</div></div>
  <div class="step"><div class="no">3</div><div class="tx"><b>突破訊號</b>：收盤 <span class="k">站上並突破最上層均線</span>（Close ＞ max(MA5,MA10,MA20,MA60)）。</div></div>
  <div class="step"><div class="no">4</div><div class="tx"><b>K棒強度</b>（避免被巴）：實體紅K（<span class="k">Close−Open ＞ ATR(${ap}) ×${am}</span>）<b>或</b>跳空（開盤 ＞ 昨高）。</div></div>
  <div class="step"><div class="no">5</div><div class="tx"><b>量能</b>：當日量 ＞ <span class="k">${va} 日均量 ×${vm}</span>（表中「量倍」欄）。</div></div>
  <div class="fml"><code>MA_Spread = (max(MA5,MA10,MA20,MA60) − min(...)) / Close × 100%</code><br>
    糾結：<code>CountIf(MA_Spread ≤ ${sq}%, ${mlb}) ≥ ${mmin}</code>　突破：<code>Close &gt; max(MA5,MA10,MA20,MA60)</code>　量能：<code>Vol &gt; MA(Vol,${va}) × ${vm}</code></div>

  <h3>② 盤整突破（唐奇安箱體）</h3>
  <p class="lead">股價在一個「<b>箱型區間</b>」（明顯的上下緣）長時間橫盤，直到某天<b>帶量向上突破箱頂</b>。</p>
  <h4>白話原理</h4>
  <p>箱頂＝上方壓力、箱底＝下方支撐。價格<b>帶量站上箱頂</b>代表買方一次把壓力吃掉、勝負分曉，往上機率高；沒量的突破常是假突破、會拉回箱內。</p>
  <h4>量化條件（需同時成立）</h4>
  <div class="step"><div class="no">1</div><div class="tx"><b>箱體量化</b>：<span class="k">箱頂 = 近 ${bd} 日最高、箱底 = 近 ${bd} 日最低</span>；箱幅％ = (箱頂 − 箱底) ÷ 收盤 ×100。</div></div>
  <div class="step"><div class="no">2</div><div class="tx"><b>時間濾網</b>：近 <span class="k">${bcb}</span> 根 K 棒中，至少 <span class="k">${bmin}</span> 根之箱幅 ≤ <span class="k">${bw}%</span>（確保長時間橫盤、非剛從大波動平復；表中「盤整K」欄）。</div></div>
  <div class="step"><div class="no">3</div><div class="tx"><b>突破訊號</b>：收盤 <span class="k">突破昨日箱頂</span>（Close ＞ Box_High[1]；用昨日箱頂，否則當日新高會同步墊高箱頂而永遠突破不了）。</div></div>
  <div class="step"><div class="no">4</div><div class="tx"><b>K棒強度</b>：實體 <span class="k">Close−Open ＞ ATR(${ap})×${am}</span> <b>且</b> 收盤位置 <span class="k">(Close−Low)/(High−Low) ≥ ${cp}</span>（收盤貼近當日高、避免長上影線假突破）。</div></div>
  <div class="step"><div class="no">5</div><div class="tx"><b>量能</b>：當日量 ＞ <span class="k">${va} 日均量 ×${vm}</span>。</div></div>
  <div class="fml"><code>Box_High = Highest(High,${bd})[1]　Box_Low = Lowest(Low,${bd})[1]</code><br>
    盤整：<code>CountIf(Box_Width ≤ ${bw}%, ${bcb}) ≥ ${bmin}</code>　突破：<code>Close &gt; Box_High[1]</code> 且 <code>(Close−Low)/(High−Low) ≥ ${cp}</code></div>

  <h3>怎麼用這兩張表</h3>
  <ul>
    <li><b>量倍</b>＝今日量 ÷ ${va} 日均量；<b>糾結度%／箱幅%</b> 越小＝整理越緊；<b>糾結K／盤整K</b>＝時間濾網達標根數（越多＝沉澱越久）。</li>
    <li><b>分數</b>：綜合「整理越緊、時間越久、量能越大、突破K棒越強」的 0–100 分，只供<b>排序</b>參考，非買賣訊號。</li>
    <li>清單<b>依概念族群分群</b>：一眼看出今天是哪一類（ABF、AI 伺服器、重電、航運…）在帶量突破；無概念退回<b>產業別</b>。點列直接開 <b>K 線圖</b>再確認籌碼。</li>
  </ul>
  <div class="warn"><b>假突破風險</b>：帶量突破後若隔天量縮、收盤跌回均線糾結區或箱頂之下，多半是假突破，應嚴設停損（例如跌破箱頂或 MA20）。這兩個是嚴格濾網，某些交易日可能掛零，屬正常。</div>
  <table>
    <tr><th>門檻</th><th>目前設定</th><th>意義</th></tr>
    <tr><td>帶量倍數</td><td>× ${vm}（÷${va}日均量）</td><td>今量相對正常量能的最低倍數</td></tr>
    <tr><td>均線糾結度</td><td>≤ ${sq}%</td><td>四線黏合的鬆緊（越小越嚴）</td></tr>
    <tr><td>糾結時間濾網</td><td>${mmin}/${mlb} 根</td><td>近${mlb}根至少幾根達糾結</td></tr>
    <tr><td>箱型幅度</td><td>≤ ${bw}%</td><td>視為「盤整」的最大箱寬</td></tr>
    <tr><td>盤整時間濾網</td><td>${bmin}/${bcb} 根</td><td>近${bcb}根至少幾根箱幅達標</td></tr>
    <tr><td>K棒實體</td><td>&gt; ATR(${ap})×${am}</td><td>突破K棒的最小力道</td></tr>
  </table>
  <p class="discl">公式整理自具公信力之台股量化資料（鉅亨、MoneyDJ、財訊、永豐豐雲學堂、QuantPass 等）之「均線糾結突破」「箱型／唐奇安盤整突破」通則，並以量化條件實作（糾結度另有標準差法、盤整另有布林頻寬 Squeeze 法可選）。門檻可依個人回測校準；本頁為研究整理，非投資建議。</p>`;
}

async function boot(){
  let d=null;
  try{ const r=await fetch("data/hui.json?v="+BUILD_V,{cache:"default"}); if(r.ok) d=await r.json(); }catch(e){}
  if(!d){
    $("today").textContent="資料尚未產生";
    ["ma","box"].forEach(k=>{ const el=$("list-"+k); if(el) el.innerHTML=`<div class="empty">尚未取得資料。<br>請先在 GitHub Actions 跑一次工作流程產生 data/hui.json。</div>`; });
    $("docbody").innerHTML=docHtml({});
    return;
  }
  $("today").textContent=d.today||"—";
  $("gentime").textContent=d.gentime||"—";
  const c=d.counts||{};
  $("cnt-ma").textContent=c.ma!=null?c.ma:(d.ma||[]).length;
  $("cnt-box").textContent=c.box!=null?c.box:(d.box||[]).length;
  const p=d.params||{};
  const setT=(id,v)=>{const e=$(id); if(e&&v!=null)e.textContent=v;};
  setT("malb-i",p.ma_conv_lookback); setT("masp-i",p.ma_spread_pct); setT("vm-i",p.vol_mult);
  setT("boxdays-i",p.box_days); setT("bcb-i",p.box_consoli_bars); setT("bw-i",p.box_width_pct);
  DATA.ma=d.ma||[]; DATA.box=d.box||[];
  ["ma","box"].forEach(renderTbl);
  $("docbody").innerHTML=docHtml(p);
}
boot();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
