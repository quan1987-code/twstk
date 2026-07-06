# -*- coding: utf-8 -*-
r"""
產業資金流向資料產生器（tw_industry_flow.py）
================================================================
讀 twstock.db（price / inst / stock / industry 表），輸出 site/data/industry.json，
供首頁「資金流向」分頁下方兩個區塊使用：

  1) 市場當天交易熱圖（heatmap）
     仿 TradingView 個股熱圖：方塊大小＝當日成交值(億)、顏色＝當日漲跌幅(台股紅漲綠跌)、
     依『產業大分類(sector)』分群。涵蓋上市＋上櫃（成交值在 price.amount 皆有）。

  2) 120 交易日產業資金輪動（rotation）
     把『概念股/產業鏈個股』整合成一個個族群（group，採 tw_industry 的概念標籤；
     無概念標籤者退回 FinMind 大分類）。每個族群算近 120 個交易日『三大法人累計買賣超金額(億)』，
     由大到小排序＝資金在這 120 天從哪些族群流出、流向哪些族群。點族群可展開成分股，
     成分股顯示方式比照『處置中個股』：今日股價、漲幅、位階、斜率(月斜)、主5、主10，
     另加（專業建議）法20(近20日法人淨額億)、量比、季乖離%。

只『讀』DB、只『寫』site/data/industry.json，不動其他資料，失敗不影響主流程。
用法：
  python tw_industry_flow.py            # 正常（需 twstock.db）
  python tw_industry_flow.py --demo     # 離線示範（合成假資料，驗證輸出/前端）
"""
import os
import sys
import json
import sqlite3
import datetime
import argparse
from statistics import pstdev

try:
    import tw_industry
except Exception:
    tw_industry = None

DB_PATH = "twstock.db"
OUT_DIR = "site"
ROT_WINDOW = 120          # 產業資金輪動回看交易日數
HEATMAP_TOPN = 160        # 熱圖最多顯示檔數（依當日成交值由大到小）
GROUP_MIN_STOCKS = 2      # 一個『族群』至少要有幾檔成分股才列入輪動圖
GROUP_STOCK_CAP = 60      # 每個族群最多列幾檔成分股（依 120 日法人淨額排序）


# ============================================================
#  指標計算（與處置頁定義一致：位階/月斜；主5/主10 改用三大法人版集中度）
# ============================================================
def _ma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def calc_stock(series, instmap):
    """series：[(date, high, low, close, vol股, amount), ...] 由舊到新（可含 None）。
    instmap：{date: (total_lots張, trust_lots張)}。
    回傳該股指標 dict；資料不足回 None。"""
    closes = [r[3] for r in series if r[3] is not None]
    if len(closes) < 2:
        return None
    last, prev = closes[-1], closes[-2]
    out = {"close": round(last, 2),
           "chg": round((last / prev - 1) * 100, 2) if prev else None}

    # 位階(wj)：20 日布林通道整數級距；月斜(yx)：MA20 一日斜率%
    if len(closes) >= 20:
        ma20 = _ma(closes, 20)
        sd = pstdev(closes[-20:])
        if sd > 0:
            out["wj"] = int(max(-10, min(10, round((last - ma20) / (2 * sd) * 10))))
        if len(closes) >= 21:
            ma20p = _ma(closes[:-1], 20)
            if ma20p:
                out["yx"] = round((ma20 / ma20p - 1) * 100, 2)

    # 季乖離%(bias60)：收盤距 60 日均線
    if len(closes) >= 60:
        ma60 = _ma(closes, 60)
        if ma60:
            out["bias60"] = round((last - ma60) / ma60 * 100, 1)

    # 量比(vr)：今量 ÷ 近 20 日均量
    vols = [r[4] for r in series if r[4] is not None]
    if len(vols) >= 21:
        avg = sum(vols[-21:-1]) / 20.0
        if avg > 0:
            out["vr"] = round(vols[-1] / avg, 2)

    # 主5/主10（三大法人集中度%）＝Σ三大法人買賣超張 ÷ Σ成交量張 ×100
    def conc(n):
        sub = series[-n:]
        sl = sv = 0.0
        has = False
        for d, _h, _l, c, v, _a in sub:
            if v:
                sv += v / 1000.0
            ti = instmap.get(d)
            if ti and ti[0] is not None:
                sl += ti[0]
                has = True
        return round(sl / sv * 100, 1) if (has and sv > 0) else None

    out["z5"] = conc(5)
    out["z10"] = conc(10)

    # 各窗『三大法人累計買賣超金額(億)』＝Σ 張 × 當日收盤 / 1e5；net20 給個股動能
    def net(n):
        sub = series[-n:]
        s = 0.0
        has = False
        for d, _h, _l, c, _v, _a in sub:
            ti = instmap.get(d)
            if ti and ti[0] is not None and c is not None:
                s += ti[0] * c / 1e5
                has = True
        return (round(s, 2), has)

    for n, key in ((5, "net5"), (20, "net20"), (60, "net60"), (ROT_WINDOW, "net120")):
        v, has = net(n)
        out[key] = v if has else None

    return out


# ============================================================
#  主流程：讀 DB → 算 → 輸出 site/data/industry.json
# ============================================================
def _sector_of(label):
    """由概念標籤推導熱圖用『大分類 sector』：有 '-' 取前段（電子上游-IC設計→電子上游）。"""
    if not label:
        return "其他"
    return label.split("-", 1)[0]


def build(con):
    # 產業標籤：group_map（概念優先）、sector_map（FinMind 大分類）
    group_map = tw_industry.label_map(con) if tw_industry else {}
    sector_map = {}
    if tw_industry:
        try:
            for sid, c in con.execute("SELECT stock_id, category FROM industry"):
                if c:
                    sector_map[sid] = tw_industry._norm_cat(c)
        except sqlite3.Error:
            pass
    names, markets = {}, {}
    for sid, nm, mk in con.execute("SELECT stock_id, name, market FROM stock"):
        names[sid] = nm or sid
        markets[sid] = mk or ""

    # 近 ROT_WINDOW 個交易日
    dates_desc = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM price ORDER BY date DESC LIMIT ?", (ROT_WINDOW,))]
    if not dates_desc:
        print("產業資金流向：price 表無資料，略過。")
        return None
    latest = dates_desc[0]
    cutoff = dates_desc[-1]

    # 一次撈進區間 price / inst，依個股組裝
    px = {}
    for sid, d, h, l, c, v, a in con.execute(
            "SELECT stock_id,date,high,low,close,volume,amount FROM price "
            "WHERE date>=? ORDER BY stock_id,date", (cutoff,)):
        px.setdefault(sid, []).append((d, h, l, c, v, a))
    inst = {}
    try:
        for sid, d, t, tr in con.execute(
                "SELECT stock_id,date,total_lots,trust_lots FROM inst WHERE date>=?", (cutoff,)):
            inst.setdefault(sid, {})[d] = (t, tr)
    except sqlite3.Error:
        pass

    # 逐股算指標 + 當日成交值(億)
    stock_ind, today_turn = {}, {}
    for sid, series in px.items():
        ind = calc_stock(series, inst.get(sid, {}))
        if ind is None:
            continue
        stock_ind[sid] = ind
        last = series[-1]
        if last[0] == latest and last[5] is not None:   # 今日成交值(億)
            today_turn[sid] = round(last[5] / 1e8, 2)

    # ---------- (1) 熱圖：依當日成交值取 TOP N，依大分類分群 ----------
    ranked = sorted(today_turn.items(), key=lambda t: -t[1])[:HEATMAP_TOPN]
    sect = {}
    for sid, turn in ranked:
        ind = stock_ind.get(sid, {})
        s = sector_map.get(sid) or _sector_of(group_map.get(sid)) or "其他"
        sect.setdefault(s, []).append({
            "sid": sid, "name": names.get(sid, sid), "turn": turn,
            "chg": ind.get("chg"), "close": ind.get("close")})
    heat_sectors = [{"name": s, "turn": round(sum(x["turn"] for x in arr), 1), "stocks": arr}
                    for s, arr in sect.items()]
    heat_sectors.sort(key=lambda g: -g["turn"])
    heatmap = {"date": latest, "n": len(ranked), "sectors": heat_sectors}

    # ---------- (2) 120 日產業資金輪動：依概念族群彙總三大法人淨額 ----------
    groups = {}
    for sid, ind in stock_ind.items():
        if sid not in inst:        # 無三大法人資料(目前上櫃)者，不計入資金輪動
            continue
        g = group_map.get(sid) or sector_map.get(sid)
        if not g:
            continue
        groups.setdefault(g, []).append(sid)

    rot_groups = []
    for g, sids in groups.items():
        if len(sids) < GROUP_MIN_STOCKS:
            continue
        agg = {"net5": 0.0, "net20": 0.0, "net60": 0.0, "net120": 0.0}
        rows = []
        for sid in sids:
            ind = stock_ind[sid]
            for k in agg:
                if ind.get(k) is not None:
                    agg[k] += ind[k]
            rows.append({
                "sid": sid, "name": names.get(sid, sid), "mkt": markets.get(sid, ""),
                "close": ind.get("close"), "chg": ind.get("chg"),
                "wj": ind.get("wj"), "yx": ind.get("yx"),
                "z5": ind.get("z5"), "z10": ind.get("z10"),
                "net20": ind.get("net20"), "vr": ind.get("vr"), "bias60": ind.get("bias60")})
        rows.sort(key=lambda r: (r["net20"] if r["net20"] is not None else 0), reverse=True)
        rot_groups.append({
            "name": g, "sector": _sector_of(g), "n": len(sids),
            "net5": round(agg["net5"], 1), "net20": round(agg["net20"], 1),
            "net60": round(agg["net60"], 1), "net120": round(agg["net120"], 1),
            "stocks": rows[:GROUP_STOCK_CAP]})
    rot_groups.sort(key=lambda g: -g["net120"])
    rotation = {"win_days": min(ROT_WINDOW, len(dates_desc)),
                "date": latest, "groups": rot_groups}

    # ---------- (3) 概念股資金輪動：依概念(一檔可複屬多概念)彙總三大法人淨額 ----------
    try:
        import tw_concepts
        cmap = tw_concepts.concept_map()
    except Exception:
        cmap = {}
    cgroups = {}
    for sid in stock_ind:
        if sid not in inst:        # 無三大法人資料者不計入
            continue
        for cpt in cmap.get(sid, []):
            cgroups.setdefault(cpt, []).append(sid)
    rot_groups_c = []
    for g, sids in cgroups.items():
        if len(sids) < GROUP_MIN_STOCKS:
            continue
        agg = {"net5": 0.0, "net20": 0.0, "net60": 0.0, "net120": 0.0}
        rows = []
        for sid in sids:
            ind = stock_ind[sid]
            for k in agg:
                if ind.get(k) is not None:
                    agg[k] += ind[k]
            rows.append({
                "sid": sid, "name": names.get(sid, sid), "mkt": markets.get(sid, ""),
                "close": ind.get("close"), "chg": ind.get("chg"),
                "wj": ind.get("wj"), "yx": ind.get("yx"),
                "z5": ind.get("z5"), "z10": ind.get("z10"),
                "net20": ind.get("net20"), "vr": ind.get("vr"), "bias60": ind.get("bias60")})
        rows.sort(key=lambda r: (r["net20"] if r["net20"] is not None else 0), reverse=True)
        rot_groups_c.append({
            "name": g, "sector": "概念股", "n": len(sids),
            "net5": round(agg["net5"], 1), "net20": round(agg["net20"], 1),
            "net60": round(agg["net60"], 1), "net120": round(agg["net120"], 1),
            "stocks": rows[:GROUP_STOCK_CAP]})
    rot_groups_c.sort(key=lambda g: -g["net120"])
    rotation_concept = {"win_days": min(ROT_WINDOW, len(dates_desc)),
                        "date": latest, "groups": rot_groups_c}

    gentime = (datetime.datetime.now(datetime.timezone.utc)
               + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    return {"date": latest, "gentime": gentime, "heatmap": heatmap,
            "rotation": rotation, "rotation_concept": rotation_concept}


def output(data):
    if not data:
        return
    d = os.path.join(OUT_DIR, "data")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "industry.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    h = data.get("heatmap", {})
    r = data.get("rotation", {})
    print(f"已輸出產業資金流向：{path}（熱圖 {h.get('n', 0)} 檔 / "
          f"{len(h.get('sectors', []))} 大分類；輪動族群 {len(r.get('groups', []))} 群）")


# ============================================================
#  離線示範：合成一份假 DB 在記憶體，驗證輸出與前端
# ============================================================
def demo_con():
    import random
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE price(stock_id TEXT,date TEXT,open REAL,high REAL,low REAL,"
                "close REAL,volume REAL,amount REAL,PRIMARY KEY(stock_id,date))")
    con.execute("CREATE TABLE stock(stock_id TEXT PRIMARY KEY,name TEXT,market TEXT)")
    con.execute("CREATE TABLE inst(stock_id TEXT,date TEXT,trust_lots REAL,foreign_lots REAL,"
                "dealer_lots REAL,total_lots REAL,PRIMARY KEY(stock_id,date))")
    con.execute("CREATE TABLE industry(stock_id TEXT PRIMARY KEY,category TEXT)")
    base = datetime.date(2026, 1, 2)
    dates = []
    d = base
    while len(dates) < ROT_WINDOW + 5:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += datetime.timedelta(days=1)
    demo = [("2330", "台積電", "上市", "半導體業", 1000, 1.0),
            ("2454", "聯發科", "上市", "半導體業", 1200, 0.8),
            ("3034", "聯詠", "上市", "電子零組件業", 500, 0.6),
            ("2317", "鴻海", "上市", "電腦及週邊設備業", 200, -0.4),
            ("2603", "長榮", "上市", "航運業", 180, -0.9),
            ("2609", "陽明", "上市", "航運業", 70, -0.7),
            ("2882", "國泰金", "上市", "金融保險業", 60, 0.2),
            ("6488", "環球晶", "上櫃", "半導體業", 600, 1.3)]
    for sid, nm, mk, cat, p0, drift in demo:
        con.execute("INSERT INTO stock VALUES(?,?,?)", (sid, nm, mk))
        con.execute("INSERT INTO industry VALUES(?,?)", (sid, cat))
        price = p0
        for i, ds in enumerate(dates):
            price = max(5, price * (1 + drift / 100 + random.uniform(-0.02, 0.02)))
            vol = random.uniform(5e6, 5e7)
            con.execute("INSERT INTO price VALUES(?,?,?,?,?,?,?,?)",
                        (sid, ds, price, price * 1.02, price * 0.98, price, vol, price * vol))
            if mk == "上市":
                tot = random.uniform(-1500, 2500) * (1 + drift)
                con.execute("INSERT INTO inst VALUES(?,?,?,?,?,?)",
                            (sid, ds, tot * 0.4, tot * 0.5, tot * 0.1, tot))
    con.commit()
    return con


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="離線示範（合成假資料）")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()
    if args.demo:
        con = demo_con()
    else:
        if not os.path.exists(args.db):
            print(f"找不到 {args.db}，略過產業資金流向（不影響主流程）。")
            return
        con = sqlite3.connect(args.db)
    try:
        output(build(con))
    except Exception as e:
        print(f"產業資金流向產出失敗（不影響主流程）：{e}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
