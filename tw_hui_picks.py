# -*- coding: utf-8 -*-
r"""
輝哥選股（tw_hui_picks.py）
================================================================
兩個「以最近一個交易日」為準的量化濾網，讀共用的 twstock.db（純價量，不需 FinMind token），
輸出 site/data/hui.json 與『單一自包含 HTML』site/hui.html。個股列表依「處置股專區」相同的
概念股分類方式分群排列（概念群在前、無概念退回產業別、未分類最後），並附一頁「公式說明」。

濾網一：均線突破四海遊龍（MA5/10/20/60 糾結後帶量突破）
  同時滿足——
   1. 收盤同時站上 5/10/20/60 日均線（四線之上）
   2. 當日成交量 ≥ 前一交易日 ×1.5（帶量）
   3. 均線糾結：突破前一日 4 條均線的最大最小差距 ÷ 收盤 ≤ 糾結門檻（四線黏合）
   （加分：收紅K／上漲，代表是「長紅突破」而非假突破）

濾網二：盤整突破（箱型整理後帶量突破箱頂）
  同時滿足——
   1. 前 N 個交易日為「區間盤整」：箱高低振幅 (箱頂−箱底)÷箱底 ≤ 箱幅門檻
   2. 收盤突破箱頂（近 N 日最高價）
   3. 當日成交量 ≥ 前一交易日 ×1.5（帶量）

公式與門檻整理自具公信力之台股財經資料（鉅亨、MoneyDJ、財訊、永豐豐雲學堂、
QuantPass 等）之「均線糾結突破」與「箱型／盤整突破」通則，重視「整理→突破→帶量」三者同時成立
以濾掉假突破。門檻以環境變數可調，預設值為常見穩健區間；本頁僅供研究，非投資建議。

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
VOL_MULT = float(os.environ.get("HUI_VOL_MULT", "1.5") or "1.5")          # 帶量：今量 ≥ 昨量 ×1.5
MA_SQUEEZE_PCT = float(os.environ.get("HUI_MA_SQUEEZE", "0.05") or "0.05")  # 四線糾結：max-min ÷ 收盤 ≤ 5%
BOX_LOOKBACK = int(os.environ.get("HUI_BOX_DAYS", "20") or "20")           # 盤整箱回看交易日
BOX_RANGE_PCT = float(os.environ.get("HUI_BOX_RANGE", "0.15") or "0.15")   # 箱型振幅 ≤ 15% 視為盤整
BOX_RANGE_MIN = float(os.environ.get("HUI_BOX_RANGE_MIN", "0.03") or "0.03")  # 箱型振幅 ≥ 3%（過度扁平者歸均線糾結，不算箱型）
MIN_PRICE = float(os.environ.get("HUI_MIN_PRICE", "8") or "8")            # 最低價（濾雞蛋水餃/流動性差）
MIN_AVG_AMT = float(os.environ.get("HUI_MIN_AVG_AMT", "20000000") or "20000000")  # 20日均額 ≥ 2000萬元
LOOKBACK_DAYS = 100                                                        # 讀取交易日數（足夠 MA60＋箱）

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


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _is_common_stock(sid):
    s = str(sid)
    return len(s) == 4 and s.isdigit() and not s.startswith("00")


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
    n_universe = 0
    for sid, arr in ser.items():
        if not _is_common_stock(sid):
            continue
        arr.sort(key=lambda x: x[0])
        if arr[-1][0] != latest:          # 最近一日沒交易（停牌/未上市）→ 不列入當日篩選
            continue
        n_universe += 1
        closes = [x[4] for x in arr]
        highs = [x[2] for x in arr]
        lows = [x[3] for x in arr]
        vols = [x[5] for x in arr]
        amts = [x[6] for x in arr]
        opens = [x[1] for x in arr]
        n = len(closes)
        if n < 2:
            continue
        close, prevc = closes[-1], closes[-2]
        vtoday, vprev, openp = vols[-1], vols[-2], opens[-1]
        if close is None or vtoday is None:
            continue

        # 流動性/價格門檻
        a20 = [a for a in amts[-20:] if a is not None]
        avg20amt = (sum(a20) / len(a20)) if a20 else 0.0
        if close < MIN_PRICE or avg20amt < MIN_AVG_AMT:
            continue

        chg = ((close / prevc - 1) * 100) if prevc else None
        volr = (vtoday / vprev) if (vprev and vprev > 0) else None
        cond_vol = (volr is not None and volr >= VOL_MULT)
        if not cond_vol:
            continue                       # 兩個濾網都要求帶量，先擋掉

        # ---- 濾網一：均線突破四海遊龍 ----
        if n >= 61:
            ma5 = _mean(closes[-5:]); ma10 = _mean(closes[-10:])
            ma20 = _mean(closes[-20:]); ma60 = _mean(closes[-60:])
            pma5 = _mean(closes[-6:-1]); pma10 = _mean(closes[-11:-1])
            pma20 = _mean(closes[-21:-1]); pma60 = _mean(closes[-61:-1])
            above = close >= ma5 and close >= ma10 and close >= ma20 and close >= ma60
            pmaxx = max(pma5, pma10, pma20, pma60)
            pminn = min(pma5, pma10, pma20, pma60)
            spread_prev = (pmaxx - pminn) / prevc if prevc else 9.9
            if above and spread_prev <= MA_SQUEEZE_PCT:
                redbody = (openp is not None and close >= openp) or (prevc is not None and close > prevc)
                tight = max(0.0, 1 - spread_prev / MA_SQUEEZE_PCT)
                volsc = min(max((volr - VOL_MULT) / VOL_MULT, 0.0), 1.0)
                score = round(100 * (0.5 * tight + 0.4 * volsc + 0.1 * (1 if redbody else 0)))
                ma_list.append({
                    "sid": sid, "close": _r(close), "chg": _r(chg), "volr": _r(volr),
                    "squeeze": _r(spread_prev * 100, 1),
                    "bias20": _r((close / ma20 - 1) * 100, 1),
                    "bias60": _r((close / ma60 - 1) * 100, 1),
                    "ma5": _r(ma5), "ma20": _r(ma20), "ma60": _r(ma60),
                    "red": bool(redbody), "score": score})

        # ---- 濾網二：盤整突破 ----
        if n >= BOX_LOOKBACK + 1:
            bh = [x for x in highs[-(BOX_LOOKBACK + 1):-1] if x is not None]
            bl = [x for x in lows[-(BOX_LOOKBACK + 1):-1] if x is not None]
            if bh and bl:
                box_high, box_low = max(bh), min(bl)
                if box_low > 0:
                    rng = (box_high - box_low) / box_low
                    if BOX_RANGE_MIN <= rng <= BOX_RANGE_PCT and close > box_high:
                        brk = (close / box_high - 1) * 100
                        tight = max(0.0, 1 - rng / BOX_RANGE_PCT)
                        volsc = min(max((volr - VOL_MULT) / VOL_MULT, 0.0), 1.0)
                        brksc = min(brk / 5.0, 1.0)
                        score = round(100 * (0.4 * tight + 0.35 * volsc + 0.25 * brksc))
                        box_list.append({
                            "sid": sid, "close": _r(close), "chg": _r(chg), "volr": _r(volr),
                            "boxHigh": _r(box_high), "boxLow": _r(box_low),
                            "boxRange": _r(rng * 100, 1), "brk": _r(brk, 1),
                            "days": BOX_LOOKBACK, "score": score})

    ma_list.sort(key=lambda x: -(x["score"] or 0))
    box_list.sort(key=lambda x: -(x["score"] or 0))
    diag["notes"].append(f"篩選基準日 {latest}・掃描 {n_universe} 檔・"
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
            "vol_mult": VOL_MULT, "ma_squeeze_pct": round(MA_SQUEEZE_PCT * 100, 1),
            "box_days": BOX_LOOKBACK, "box_range_pct": round(BOX_RANGE_PCT * 100, 1),
            "box_range_min_pct": round(BOX_RANGE_MIN * 100, 1),
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
    def ma(sid, name, mkt, close, chg, volr, squeeze, b20, b60, score, ind="", cpt=None):
        return {"sid": sid, "name": name, "mkt": mkt, "close": close, "chg": chg, "volr": volr,
                "squeeze": squeeze, "bias20": b20, "bias60": b60, "red": True, "score": score,
                "ind": ind, "cpt": cpt or []}

    def bx(sid, name, mkt, close, chg, volr, bh, bl, brk, rng, score, ind="", cpt=None):
        return {"sid": sid, "name": name, "mkt": mkt, "close": close, "chg": chg, "volr": volr,
                "boxHigh": bh, "boxLow": bl, "brk": brk, "boxRange": rng, "days": BOX_LOOKBACK,
                "score": score, "ind": ind, "cpt": cpt or []}

    ma_list = [
        ma("3037", "欣興", "上市", 205.0, 6.2, 2.4, 1.8, 3.1, 4.5, 92, cpt=["ABF載板", "PCB"]),
        ma("8046", "南電", "上市", 158.0, 5.1, 2.1, 2.4, 2.7, 3.9, 84, cpt=["ABF載板"]),
        ma("2330", "台積電", "上市", 940.0, 3.4, 1.9, 2.9, 2.1, 3.0, 78, cpt=["CoWoS/先進封裝"]),
        ma("2454", "聯發科", "上市", 1180.0, 4.0, 1.7, 3.4, 2.6, 3.3, 72, cpt=["IC設計"]),
        ma("1519", "華城", "上市", 620.0, 5.6, 2.6, 2.2, 3.8, 5.1, 88, cpt=["重電"]),
        ma("2049", "上銀", "上市", 245.0, 3.1, 1.6, 3.9, 1.9, 2.4, 61, cpt=["工具機", "機器人/自動化"]),
        ma("9958", "世紀鋼", "上市", 188.0, 4.3, 1.8, 2.7, 2.2, 3.0, 70, cpt=["風電", "綠能/儲能"]),
        ma("1234", "黑松", "上市", 43.2, 3.6, 1.9, 3.2, 2.8, 3.5, 58, ind="食品"),
        ma("5871", "中租-KY", "上市", 168.0, 2.9, 1.6, 4.1, 1.7, 2.0, 52, ind="其他"),
    ]
    box_list = [
        bx("3231", "緯創", "上市", 128.5, 4.8, 2.3, 122.0, 108.0, 5.3, 13.0, 90, cpt=["AI伺服器"]),
        bx("2382", "廣達", "上市", 305.0, 3.9, 2.0, 296.0, 268.0, 3.0, 10.4, 80, cpt=["AI伺服器"]),
        bx("3661", "世芯-KY", "上市", 3120.0, 5.4, 1.9, 2980.0, 2650.0, 4.7, 12.5, 83, cpt=["IP矽智財", "IC設計"]),
        bx("6533", "晶心科", "上櫃", 512.0, 6.1, 2.5, 486.0, 430.0, 5.3, 13.0, 86, cpt=["IP矽智財", "IC設計"]),
        bx("2603", "長榮", "上市", 235.0, 4.4, 2.2, 224.0, 200.0, 4.9, 12.0, 79, cpt=["航運(貨櫃/散裝)"]),
        bx("1256", "鮮活果汁-KY", "上櫃", 158.0, 3.7, 1.7, 152.0, 138.0, 3.9, 10.1, 55, ind="食品"),
        bx("2882", "國泰金", "上市", 68.5, 2.6, 1.6, 66.8, 60.5, 2.5, 10.4, 48, cpt=["金融"]),
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
  .doc .fml{background:var(--card2); border:1px solid var(--border); border-radius:9px; padding:11px 13px; margin:10px 0; font-size:12.5px; color:var(--muted); line-height:1.7;}
  .doc .fml b{color:var(--text);}
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
    <div class="intro"><b>均線突破四海遊龍</b>：<span class="k">5/10/20/60 日均線糾結</span>（四線黏合、籌碼沉澱）後，
      收盤<span class="k">同時站上四條均線</span>、且<span class="k">當日量 ≥ 前一日 ×1.5</span> 帶量突破。糾結度越小＝四線越黏、突破越乾淨。</div>
    <div class="cnt">符合 <b id="cnt-ma">—</b> 檔（依概念族群分群；點列看 K 線・點欄位標題排序）</div>
    <div id="list-ma"></div>
  </div>

  <div class="pane hidden" id="p-box">
    <div class="intro"><b>盤整突破</b>：前 <span class="k" id="boxdays-i">20</span> 個交易日在
      <span class="k">箱型區間盤整</span>（高低振幅 ≤ 門檻），今日收盤<span class="k">帶量突破箱頂</span>
      （量 ≥ 前一日 ×1.5）。突破幅％＝收盤高出箱頂多少；箱幅％越小＝盤整越緊、突破越有力。</div>
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
  sqz:    v=>isNum(v)?`<span class="cv">${Number(v).toFixed(1)}%</span>`:"—",
  plain:  v=>isNum(v)?`<span class="cv">${price(v)}</span>`:"—",
  brk:    v=>isNum(v)?`<span class="cv up">+${Number(v).toFixed(1)}%</span>`:"—",
  rng:    v=>isNum(v)?`<span class="cv">${Number(v).toFixed(1)}%</span>`:"—",
  score:  v=>isNum(v)?`<span class="cv amb">${Math.round(v)}</span>`:"—",
};
const COLS = {
  ma: [["收盤","close",FMT.price],["漲跌%","chg",FMT.pct],["量比","volr",FMT.volr],
       ["糾結度%","squeeze",FMT.sqz],["距月線%","bias20",FMT.pct1],["距季線%","bias60",FMT.pct1],["分數","score",FMT.score]],
  box:[["收盤","close",FMT.price],["漲跌%","chg",FMT.pct],["量比","volr",FMT.volr],
       ["箱頂","boxHigh",FMT.plain],["突破幅%","brk",FMT.brk],["箱幅%","boxRange",FMT.rng],["分數","score",FMT.score]],
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
  if(!data.length){ el.innerHTML=`<div class="empty">最近一個交易日沒有符合「${name==="ma"?"均線突破四海遊龍":"盤整突破"}」條件的個股。<br><span class="dim">市場清淡或多數股尚未帶量突破時屬正常；換一個交易日再看。</span></div>`; return; }
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
  const vm=p.vol_mult||1.5, sq=p.ma_squeeze_pct||5, bd=p.box_days||20, br=p.box_range_pct||15, bmin=p.box_range_min_pct||3;
  return `
  <h3>兩種濾網在找什麼？</h3>
  <p class="lead">兩個都是「<b style="color:var(--text)">先沉澱、再帶量突破</b>」的起漲型態：股價先進入一段「大家看法僵持、量縮盤整」的階段，把浮動籌碼洗乾淨；一旦有主力進場帶量往上拉，往往就是一段行情的起點。差別只在「怎麼定義沉澱」——一個用<b>均線黏合</b>，一個用<b>箱型區間</b>。兩者都要求「<span class="k">整理 → 突破 → 帶量</span>」三者<b>同時</b>成立，用來濾掉沒有量、撐不住的假突破。</p>

  <h3>① 均線突破四海遊龍</h3>
  <p class="lead">「四海遊龍」＝ <b>5、10、20、60 四條均線</b>先「糾結」在一起（像四條龍盤在一起休息），再一根長紅同時衝上四條線、飛龍在天。</p>
  <h4>白話原理</h4>
  <p>當短中長期均線都黏在一起，代表這段期間股價幾乎沒漲沒跌、成本墊高得差不多，<b>沒信心的人會慢慢退場、籌碼變乾淨</b>。此時只要有一方（通常是主力）帶量把股價往上拉，上方套牢賣壓很少，股價很容易一路噴上去。</p>
  <h4>我們的量化條件（需同時成立）</h4>
  <div class="step"><div class="no">1</div><div class="tx"><b>站上四線</b>：收盤同時 ≥ MA5、MA10、MA20、MA60（<b>價格站上 5/10/20/60 日線</b>）。</div></div>
  <div class="step"><div class="no">2</div><div class="tx"><b>帶量</b>：當日成交量 ≥ 前一交易日 × <span class="k">${vm}</span>。</div></div>
  <div class="step"><div class="no">3</div><div class="tx"><b>均線糾結</b>：突破前一日，四條均線的「最高−最低」差距 ÷ 收盤 ≤ <span class="k">${sq}%</span>（四線黏合＝糾結度小）。</div></div>
  <div class="step"><div class="no">＋</div><div class="tx"><b>長紅加分</b>：收紅K（收盤 ≥ 開盤）或上漲，代表是「長紅突破」而非帶量卻收黑的假突破。</div></div>
  <div class="fml"><b>糾結度%</b> ＝ (max(MA5,MA10,MA20,MA60) − min(MA5,MA10,MA20,MA60)) ÷ 收盤 ×100，<b>越小＝四條均線越黏</b>、突破越乾淨。<br>
    <b>距月線% / 距季線%</b> ＝ 收盤相對 MA20 / MA60 的乖離，數字太大代表短線已漲多、追高風險高。</div>

  <h3>② 盤整突破</h3>
  <p class="lead">股價在一個「<b>箱型區間</b>」（有明顯的上下緣）來回震盪一段時間，直到某天<b>帶量向上突破箱頂</b>。</p>
  <h4>白話原理</h4>
  <p>箱型盤整＝多空在一個固定區間拉鋸。箱頂是「上方壓力」、箱底是「下方支撐」。當價格<b>帶量站上箱頂</b>，代表買方一次把壓力吃掉、勝負分曉，後續往上攻的機率高；沒有量的突破常常是假突破，會拉回箱內。</p>
  <h4>我們的量化條件（需同時成立）</h4>
  <div class="step"><div class="no">1</div><div class="tx"><b>區間盤整</b>：前 <span class="k">${bd}</span> 個交易日「箱頂 − 箱底」振幅 ÷ 箱底介於 <span class="k">${bmin}% ~ ${br}%</span>（夠窄才算盤整、不是趨勢盤；<b>過度扁平（&lt;${bmin}%）歸類為均線糾結</b>，避免兩張表重複）。</div></div>
  <div class="step"><div class="no">2</div><div class="tx"><b>突破箱頂</b>：今日收盤 > 前 ${bd} 日的最高價（箱頂），創區間新高。</div></div>
  <div class="step"><div class="no">3</div><div class="tx"><b>帶量</b>：當日成交量 ≥ 前一交易日 × <span class="k">${vm}</span>。</div></div>
  <div class="fml"><b>箱頂</b> ＝ 前 ${bd} 日最高價；<b>箱底</b> ＝ 前 ${bd} 日最低價。<br>
    <b>箱幅%</b> ＝ (箱頂 − 箱底) ÷ 箱底 ×100，<b>越小＝盤整越緊</b>、突破越有力。<br>
    <b>突破幅%</b> ＝ (收盤 ÷ 箱頂 − 1) ×100，收盤高出箱頂越多、突破越確定（一般 ≥3% 視為較有效的突破）。</div>

  <h3>怎麼用這兩張表</h3>
  <ul>
    <li><b>分數</b>：綜合「整理越緊、量能越大、突破越乾淨」給的 0–100 分，只供<b>排序</b>參考，不是買賣訊號。</li>
    <li>清單<b>依概念族群分群</b>：一眼看出今天是哪一類（ABF、AI 伺服器、重電、航運…）在帶量突破；沒有概念標籤的退回<b>產業別</b>。</li>
    <li>點任一列直接跳到該股 <b>K 線圖</b>，用副圖的主力買賣超／外資／400 張大戶再確認籌碼。</li>
  </ul>
  <div class="warn"><b>假突破風險</b>：帶量突破後若隔天量縮、收盤跌回均線糾結區或箱頂之下，多半是假突破，應嚴設停損（例如跌破箱頂或 MA20）。突破當天追高，務必控制部位與停損。</div>
  <table>
    <tr><th>門檻</th><th>目前設定</th><th>意義</th></tr>
    <tr><td>帶量倍數</td><td>× ${vm}</td><td>今量 ÷ 昨量 的最低要求</td></tr>
    <tr><td>均線糾結度</td><td>≤ ${sq}%</td><td>四線黏合的鬆緊（越小越嚴）</td></tr>
    <tr><td>盤整回看</td><td>${bd} 日</td><td>箱型取樣的交易日數</td></tr>
    <tr><td>箱型振幅</td><td>${bmin}% ~ ${br}%</td><td>視為「盤整」的區間寬度（過扁歸均線糾結）</td></tr>
  </table>
  <p class="discl">公式與門檻整理自具公信力之台股財經資料（鉅亨、MoneyDJ、財訊、永豐豐雲學堂、QuantPass 等）之「均線糾結突破」「箱型／盤整突破」通則，並以量化條件實作。門檻可依個人回測校準；本頁為研究整理，非投資建議。</p>`;
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
  const bd=(d.params&&d.params.box_days)||20; const bdi=$("boxdays-i"); if(bdi) bdi.textContent=bd;
  DATA.ma=d.ma||[]; DATA.box=d.box||[];
  ["ma","box"].forEach(renderTbl);
  $("docbody").innerHTML=docHtml(d.params||{});
}
boot();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
