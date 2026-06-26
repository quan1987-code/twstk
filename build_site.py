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
import glob
import json
import sqlite3
import datetime
import pandas as pd

try:
    import yfinance as yf
except Exception:
    yf = None

DB_PATH = "twstock.db"
LOOKBACK_BARS = 1000
OUT_DIR = "site"

# 首頁三標的：(代碼, yfinance符號, 顯示名, 數值型態 'index'整數 / 'price'兩位小數)
MARKET_TARGETS = [
    ("TWII", "^TWII", "台股加權指數", "index"),
    ("SOX",  "^SOX",  "費城半導體 SOX", "index"),
    ("TSMC", "2330.TW", "台積電 2330", "price"),
]


def find_latest_csv():
    cands = glob.glob(os.path.join("output", "breakout_*.csv")) or glob.glob("breakout_*.csv")
    return max(cands, key=os.path.getmtime) if cands else None


def find_latest_trust():
    cands = glob.glob(os.path.join("output", "trust_*.json")) or glob.glob("trust_*.json")
    return max(cands, key=os.path.getmtime) if cands else None


def load_trust():
    tp = find_latest_trust()
    if not tp:
        return {}
    try:
        with open(tp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_history(stock_ids, db_path):
    hist = {}
    if not os.path.exists(db_path):
        return hist, False
    con = sqlite3.connect(db_path)
    for sid in stock_ids:
        try:
            rows = con.execute(
                "SELECT date,open,high,low,close,volume FROM price "
                "WHERE stock_id=? ORDER BY date DESC LIMIT ?", (sid, LOOKBACK_BARS)).fetchall()
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


def fetch_yf(symbol):
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
        return _drawdown(dates, highs, closes)
    except Exception as e:
        print(f"  yfinance 抓 {symbol} 失敗：{e}")
        return None


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
        r = fetch_yf(sym)
        if r is None and code == "TSMC":
            r = tsmc_from_db()
        if r is not None:
            r["name"] = name
            r["kind"] = kind
            print(f"  市場資料 {name}：最高 {r['ath']}（{r['ath_date']}）/ 最近 {r['last']}（{r['last_date']}）/ 回撤 {r['dd']}%")
        else:
            print(f"  市場資料 {name}：取得失敗")
        out[code] = r
    return out


def build_html(results, history, market, trust, date, count, db_ok, gentime):
    return (TEMPLATE
            .replace("/*__RESULTS__*/null", json.dumps(results, ensure_ascii=False))
            .replace("/*__HISTORY__*/null", json.dumps(history, ensure_ascii=False))
            .replace("/*__MARKET__*/null", json.dumps(market, ensure_ascii=False))
            .replace("/*__TRUST__*/null", json.dumps(trust, ensure_ascii=False))
            .replace("/*__DBOK__*/false", "true" if db_ok else "false")
            .replace("__DATE__", date or "")
            .replace("__GENTIME__", gentime)
            .replace("__COUNT__", str(count)))


def write_page(results, history, market, trust, date, db_ok, gentime):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_html(results, history, market, trust, date, len(results), db_ok, gentime))
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "台股看板", "short_name": "台股看板", "display": "standalone",
                   "orientation": "portrait", "background_color": "#0a0f1a",
                   "theme_color": "#0a0f1a", "start_url": "."}, f, ensure_ascii=False)


def main():
    gentime = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    print("抓首頁市場資料（加權指數 / 費半 / 台積電）…")
    market = get_market()
    trust = load_trust()

    path = sys.argv[1] if len(sys.argv) > 1 else find_latest_csv()
    if not path or not os.path.exists(path):
        print("找不到選股 CSV，第二分頁顯示空清單（首頁與投信頁仍正常）。")
        write_page([], {}, market, trust, "", False, gentime)
        return
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    results = df.to_dict(orient="records")
    date = results[0].get("資料日", "") if results else ""
    ids = [r.get("代號", "") for r in results if r.get("代號")]
    history, db_ok = load_history(ids, DB_PATH)
    write_page(results, history, market, trust, date, db_ok, gentime)
    tcount = len(trust.get("data", {})) if isinstance(trust, dict) else 0
    print(f"已產生 {OUT_DIR}/index.html（爆量 {len(results)} 檔・投信候選 {tcount} 檔・資料日 {date}・更新 {gentime}）")


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
<meta name="theme-color" content="#0a0f1a">
<link rel="manifest" href="manifest.json">
<title>台股看板 ・ __DATE__</title>
<style>
  :root{
    --bg:#0a0f1a; --card:#111827; --card2:#161f33; --border:#1f2a3d;
    --text:#e6edf6; --muted:#93a3b8; --dim:#5e6f86;
    --amber:#f5a524; --amber-s:rgba(245,165,36,.14);
    --up:#ff4d4f; --down:#22c55e;
    --blue:#4d9fff; --blue-s:rgba(77,159,255,.12);
    --purple:#b794ff; --purple-s:rgba(183,148,255,.12);
    --ma5:#f5c518; --ma10:#e23fd0; --ma20:#27c4dc; --ma60:#c79a52; --ma240:#3b6fe0;
  }
  *{box-sizing:border-box;}
  body{margin:0; background:var(--bg); color:var(--text);
    font-family:'Inter','Noto Sans TC','PingFang TC','Microsoft JhengHei',system-ui,sans-serif;
    -webkit-font-smoothing:antialiased; padding:16px 12px 36px; padding-top:calc(16px + env(safe-area-inset-top));}
  .num{font-variant-numeric:tabular-nums;}
  .wrap{max-width:1180px; margin:0 auto;}
  header h1{font-size:19px; font-weight:800; margin:0;}
  header h1 .bolt{color:var(--amber);}
  .sub{font-size:12px; color:var(--muted); margin-top:4px;}
  .hidden{display:none !important;}

  .tabbar{display:flex; gap:6px; margin:14px 0; background:var(--card); padding:5px; border-radius:11px; border:1px solid var(--border);}
  .tab{flex:1; background:transparent; color:var(--muted); border:none; border-radius:8px; padding:10px 8px; font-size:14px; font-weight:700; cursor:pointer;}
  .tab.on{background:var(--amber-s); color:var(--amber);}

  /* 首頁回撤卡 */
  .ddcards{display:grid; grid-template-columns:1fr; gap:12px;}
  .ddcard{background:var(--card); border:1px solid var(--border); border-radius:13px; padding:16px 17px;}
  .ddname{font-size:15px; font-weight:800; margin-bottom:10px;}
  .ddbig{font-size:13px; color:var(--muted); margin-bottom:10px;}
  .ddbig b{font-size:30px; font-weight:800; color:var(--amber); margin-left:4px; letter-spacing:.3px;}
  .ddbig b.flat{color:var(--up);}
  .ddbar{height:8px; background:#0e1626; border-radius:5px; overflow:hidden; margin-bottom:13px;}
  .ddbarfill{height:100%; background:linear-gradient(90deg,#f5a524,#e0701f); border-radius:5px;}
  .ddrow{display:flex; align-items:baseline; gap:8px; padding:4px 0; border-top:1px solid var(--border);}
  .ddrow .k{font-size:12px; color:var(--dim); width:96px; flex:none;}
  .ddrow .v{font-size:17px; font-weight:800; font-variant-numeric:tabular-nums;}
  .ddrow .d{font-size:11px; color:var(--muted); margin-left:auto;}
  .ddna{color:var(--dim); font-size:13px; padding:8px 0;}
  .ddnote{font-size:12px; color:var(--dim); line-height:1.6; margin-top:14px; padding:13px 15px; background:var(--card); border:1px solid var(--border); border-radius:11px;}

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
  .hint{font-size:11px; color:var(--dim); width:100%;}
  .tablewrap{overflow-x:auto; -webkit-overflow-scrolling:touch; border:1px solid var(--border); border-radius:12px; background:var(--card);}
  table{width:100%; border-collapse:collapse; font-size:13px; min-width:860px;}
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

  #cv{position:fixed; inset:0; background:#070b14; z-index:50; display:none; flex-direction:column; padding-top:env(safe-area-inset-top);}
  #cv.open{display:flex;}
  .cvhead{display:flex; align-items:center; gap:10px; padding:10px 14px; border-bottom:1px solid var(--border); flex-wrap:wrap;}
  .back{background:var(--card); border:1px solid var(--border); color:var(--muted); border-radius:8px; padding:7px 12px; font-size:14px; cursor:pointer;}
  .cvtitle{font-size:17px; font-weight:800;}
  .cvtitle .c{color:var(--amber); margin-right:7px;}
  .cvchg{font-size:14px; font-weight:700;}
  .pswitch{display:flex; gap:4px; margin-left:auto;}
  .pbtn{background:var(--card); border:1px solid var(--border); color:var(--muted); border-radius:7px; padding:7px 15px; font-size:14px; cursor:pointer; font-weight:600;}
  .pbtn.on{background:var(--amber-s); color:var(--amber); border-color:rgba(245,165,36,.4);}
  .readout{display:flex; gap:13px; flex-wrap:wrap; padding:8px 14px; font-size:13px; border-bottom:1px solid var(--border); background:var(--card);}
  .readout .it{display:flex; gap:5px;}
  .readout .k{color:var(--dim);}
  .readout .v{font-weight:700; font-variant-numeric:tabular-nums;}
  .malegend{display:flex; gap:11px; flex-wrap:wrap; padding:6px 14px 0; font-size:11px;}
  .malegend span{display:flex; align-items:center; gap:4px; color:var(--muted);}
  .malegend i{width:13px; height:3px; border-radius:2px; display:inline-block;}
  .chartbox{flex:1; position:relative; min-height:0;}
  #chartCanvas{position:absolute; inset:0; width:100%; height:100%; touch-action:none;}
  @media(min-width:640px){ .cards{grid-template-columns:repeat(4,1fr);} .ddcards{grid-template-columns:repeat(3,1fr);} body{padding:22px 18px 40px;} }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1><span class="bolt">⚡</span> 台股看板</h1>
    <div class="sub">更新 __GENTIME__（台北）</div>
  </header>

  <div class="tabbar">
    <button class="tab on" data-tab="home">首頁回撤</button>
    <button class="tab" data-tab="screen">爆量起漲</button>
    <button class="tab" data-tab="trust">投信連買</button>
  </div>

  <!-- 分頁一：首頁 -->
  <div class="tabpane" id="tab-home">
    <div class="ddcards" id="ddcards"></div>
    <div class="ddnote">
      <b style="color:var(--muted)">回撤</b>＝最近一次收盤距「歷史最高價（盤中最高點）」的跌幅。<br>
      數字越大代表離前高越遠。台股加權指數與費城半導體為指數點數，台積電為股價。<br>
      美股費半依美國收盤，台北下午更新時通常為「前一個美股交易日」。
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
      <span class="hint">點欄位排序 ・ 點名稱看K線</span>
    </div>
    <div class="tablewrap"><table><thead><tr id="thead"></tr></thead><tbody id="tbody"></tbody></table></div>
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
        <div><b>評分</b>：以「投信主導性(佔比)」為核心，加吃貨強度、補漲空間、貼近投信成本；已大漲者扣分。<b>僅供排序，非投資建議</b>。</div>
        <div style="color:var(--dim)">註：投信買賣超為盤後資料，通常較股價晚約一個交易日；目前涵蓋上市，上櫃稍後補上。</div>
      </div>
    </details>
    <div class="tablewrap"><table><thead><tr id="trusthead"></tr></thead><tbody id="trustbody"></tbody></table></div>
  </div>

  <div class="foot">資料：證交所/櫃買 + FinMind + Yahoo ・ 僅供研究，非投資建議</div>
</div>

<div id="cv">
  <div class="cvhead">
    <button class="back" onclick="closeChart()">◀ 返回</button>
    <div class="cvtitle"><span class="c" id="cvCode"></span><span id="cvName"></span></div>
    <div class="cvchg" id="cvChg"></div>
    <div class="pswitch"><button class="pbtn on" data-p="D">日K</button><button class="pbtn" data-p="W">週K</button><button class="pbtn" data-p="M">月K</button></div>
  </div>
  <div class="readout" id="readout"></div>
  <div class="malegend">
    <span><i style="background:var(--ma5)"></i>MA5</span><span><i style="background:var(--ma10)"></i>MA10</span>
    <span><i style="background:var(--ma20)"></i>MA20</span><span><i style="background:var(--ma60)"></i>MA60</span>
    <span><i style="background:var(--ma240)"></i>MA240</span><span><i style="background:#8aa0b6"></i>布林20</span>
    <span style="color:var(--dim)">單指拖移看讀數 ・ 雙指縮放</span>
  </div>
  <div class="chartbox"><canvas id="chartCanvas"></canvas></div>
</div>

<script>
const RESULTS = /*__RESULTS__*/null;
const HISTORY = /*__HISTORY__*/null;
const MARKET  = /*__MARKET__*/null;
const TRUST   = /*__TRUST__*/null;
const DB_OK   = /*__DBOK__*/false;

/* ---------- 分頁切換 ---------- */
const PANES = ["home", "screen", "trust"];
document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("on")); t.classList.add("on");
  const id=t.dataset.tab;
  PANES.forEach(p=>document.getElementById("tab-"+p).classList.toggle("hidden", p!==id));
}));

/* ---------- 首頁：市場回撤卡 ---------- */
function renderDD(){
  const order=["TWII","SOX","TSMC"];
  document.getElementById("ddcards").innerHTML=order.map(k=>{
    const m=MARKET?MARKET[k]:null;
    if(!m) return `<div class="ddcard"><div class="ddname">${k==="TWII"?"台股加權指數":k==="SOX"?"費城半導體 SOX":"台積電 2330"}</div><div class="ddna">資料暫時無法取得</div></div>`;
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

/* ---------- 投信連買：依門檻即時運算 + 強弱排序 ---------- */
let trustThr = 50;
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
    score = Math.max(0, Math.min(100, score));
    rows.push({sid, name:o.name, market:o.market, days, dominance, total, lastClose, cost, costBias, hi, gapHigh, streakRet, stillBuying, score});
  }
  rows.sort((x,y)=>y.score-x.score);
  return rows;
}
function renderTrust(){
  document.getElementById("trustdate").textContent = (TRUST && TRUST.date) ? TRUST.date : "—";
  const cols=["代號","名稱","市場","連買(天)","投信買超佔比","連買累計(張)","現價","投信成本","距成本%","連買最高","距高點%","連買漲幅%","仍在買","評分"];
  document.getElementById("trusthead").innerHTML = cols.map((c,i)=>`<th class="${i<2?'l':''}">${c}</th>`).join("");
  const tb=document.getElementById("trustbody");
  if(!TRUST || !TRUST.data || !Object.keys(TRUST.data).length){
    tb.innerHTML=`<tr><td colspan="14" style="text-align:center;color:var(--dim);padding:36px">投信資料準備中（下次自動更新後出現）</td></tr>`; return;
  }
  const rows = computeTrustRows(trustThr);
  if(!rows.length){
    tb.innerHTML=`<tr><td colspan="14" style="text-align:center;color:var(--dim);padding:36px">此門檻下沒有符合「投信連買 ≥3 日且尚未漲上去」的個股<br>可試試降低每日張數門檻</td></tr>`; return;
  }
  const pct=(v)=>(v>=0?"+":"")+(v*100).toFixed(1)+"%";
  tb.innerHTML = rows.map(r=>{
    const domc = r.dominance>=0.25?"var(--amber)":r.dominance>=0.12?"#d98818":"var(--text)";
    const costc = r.costBias<=0?"var(--down)":"var(--up)";
    const scC = r.score>=70?"var(--up)":r.score>=45?"var(--amber)":"var(--dim)";
    const mkt = r.market==="上市"?"twse":"tpex";
    const has = (HISTORY&&HISTORY[r.sid])?`onclick="openChart('${r.sid}')"`:'';
    return `<tr>
      <td class="l"><span class="code">${r.sid}</span></td>
      <td class="l"><span class="nm" ${has}>${r.name||""}</span></td>
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
      <td class="num">${r.stillBuying?'<span style="color:var(--up)">是</span>':'<span style="color:var(--dim)">—</span>'}</td>
      <td><span class="scorewrap"><span class="scoretrack"><span class="scorefill" style="width:${Math.min(r.score,100)}%;background:${scC}"></span></span><span class="scoreval" style="color:${scC}">${r.score.toFixed(0)}</span></span></td>
    </tr>`;
  }).join("");
}
document.querySelectorAll(".thrbtn").forEach(b=>b.addEventListener("click",()=>{
  document.querySelectorAll(".thrbtn").forEach(x=>x.classList.remove("on")); b.classList.add("on");
  trustThr=parseInt(b.dataset.thr,10); renderTrust();
}));

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
  ["成交量(張)",""],["月均量(張)",""],["量比",""],["5日量/月量",""],["季線乖離%",""],["評分",""],["強度標記","l"]];
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
  document.querySelectorAll("th").forEach(th=>th.onclick=()=>{const k=th.dataset.k; if(state.sort===k)state.asc=!state.asc; else {state.sort=k;state.asc=false;} render();});
}
function renderTable(d){
  document.getElementById("tbody").innerHTML=d.map(r=>{
    const chg=num(r["漲跌%"]), cc=chg>0?"var(--up)":chg<0?"var(--down)":"var(--muted)";
    const lim=chg>=9.5?`<span class="lim" style="background:var(--up)">漲停</span>`:(chg<=-9.5?`<span class="lim" style="background:var(--down)">跌停</span>`:"");
    const vr=num(r["量比"])||0, vc=vr>=3?"var(--amber)":vr>=2?"#d98818":"var(--text)";
    const sv=num(r["評分"])||0, scC=sv>=70?"var(--up)":sv>=45?"var(--amber)":"var(--dim)", mkt=r["市場"]==="上市"?"twse":"tpex";
    const has=(HISTORY&&HISTORY[r["代號"]])?`onclick="openChart('${r["代號"]}')"`:'title="無歷史資料"';
    const tags=(r["強度標記"]||"").split("·").filter(Boolean).map(t=>{const c=TAGS[t]||["rgba(94,111,134,.15)","#93a3b8","rgba(94,111,134,.3)"]; return `<span class="tag" style="background:${c[0]};color:${c[1]};border-color:${c[2]}">${t}</span>`;}).join("");
    return `<tr><td class="l"><span class="code">${r["代號"]}</span></td><td class="l"><span class="nm" ${has}>${r["名稱"]||""}</span></td>
      <td><span class="mkt ${mkt}">${r["市場"]}</span></td><td class="num">${r["收盤"]}</td>
      <td class="num" style="color:${cc};font-weight:700">${chg>0?"+":""}${r["漲跌%"]}%${lim}</td>
      <td class="num">${fmtInt(r["成交量(張)"])}</td><td class="num" style="color:var(--muted)">${fmtInt(r["月均量(張)"])}</td>
      <td class="num"><span class="vr" style="color:${vc}">${r["量比"]}x</span></td><td class="num">${r["5日量/月量"]}</td>
      <td class="num">${r["季線乖離%"]}%</td>
      <td><span class="scorewrap"><span class="scoretrack"><span class="scorefill" style="width:${Math.min(sv,100)}%;background:${scC}"></span></span><span class="scoreval" style="color:${scC}">${r["評分"]}</span></span></td>
      <td class="tags">${tags}</td></tr>`;
  }).join("")||`<tr><td colspan="12" style="text-align:center;color:var(--dim);padding:36px">今日無符合條件的標的</td></tr>`;
}
function render(){const d=view(); renderCards(d); renderBars(d); renderHead(); renderTable(d);
  document.getElementById("subtitle").textContent=`資料日 __DATE__ ・ 顯示 ${d.length} 檔`;}
document.getElementById("q").addEventListener("input",e=>{state.q=e.target.value; render();});
document.querySelectorAll(".chip").forEach(c=>c.addEventListener("click",()=>{document.querySelectorAll(".chip").forEach(x=>x.classList.remove("on")); c.classList.add("on"); state.mkt=c.dataset.mkt; render();}));

/* ===== 技術線圖引擎 ===== */
const MACOLOR={5:"#f5c518",10:"#e23fd0",20:"#27c4dc",60:"#c79a52",240:"#3b6fe0"};
const PRICE_MAS=[5,10,20,60,240], VOL_MAS=[5,20,60];
const UP="#ff4d4f", DOWN="#22c55e", BOLL="#8aa0b6";
function SMA(a,n){const o=new Array(a.length).fill(null);let s=0,cnt=0; for(let i=0;i<a.length;i++){if(a[i]==null){o[i]=null;continue;} s+=a[i];cnt++; if(i>=n&&a[i-n]!=null){s-=a[i-n];cnt--;} if(cnt>=n)o[i]=s/n;} return o;}
function EMA(a,n){const o=new Array(a.length).fill(null);const k=2/(n+1);let p=null; for(let i=0;i<a.length;i++){if(a[i]==null){o[i]=p;continue;} p=(p==null)?a[i]:a[i]*k+p*(1-k); o[i]=p;} return o;}
function STD(a,n,ma){const o=new Array(a.length).fill(null); for(let i=n-1;i<a.length;i++){if(ma[i]==null)continue;let s=0,ok=true;for(let j=i-n+1;j<=i;j++){if(a[j]==null){ok=false;break;}const d=a[j]-ma[i];s+=d*d;} if(ok)o[i]=Math.sqrt(s/n);} return o;}
function MACD(c){const e12=EMA(c,12),e26=EMA(c,26); const dif=c.map((_,i)=>(e12[i]!=null&&e26[i]!=null&&i>=25)?e12[i]-e26[i]:null);
  const dea=new Array(c.length).fill(null);const k=2/10;let p=null; for(let i=0;i<dif.length;i++){if(dif[i]==null)continue; p=(p==null)?dif[i]:dif[i]*k+p*(1-k); dea[i]=p;}
  const osc=dif.map((d,i)=>(d!=null&&dea[i]!=null)?d-dea[i]:null); return {dif,dea,osc};}
function aggregate(daily, period){
  if(period==="D") return daily.map(b=>({d:b[0],o:b[1],h:b[2],l:b[3],c:b[4],v:b[5]}));
  const key=(s)=>{ if(period==="M") return s.slice(0,7); const p=s.split("-").map(Number); const dt=new Date(Date.UTC(p[0],p[1]-1,p[2])); const day=(dt.getUTCDay()+6)%7; dt.setUTCDate(dt.getUTCDate()-day); return dt.toISOString().slice(0,10); };
  const map={}, order=[];
  for(const b of daily){ const k=key(b[0]); if(!map[k]){ map[k]={d:b[0],o:b[1],h:b[2],l:b[3],c:b[4],v:b[5]}; order.push(k);} else { const g=map[k]; g.h=Math.max(g.h,b[2]); g.l=Math.min(g.l,b[3]); g.c=b[4]; g.v+=b[5]; g.d=b[0]; } }
  return order.map(k=>map[k]);
}
let CH={ sid:null, period:"D", bars:[], ind:null, count:90, offset:0, hover:null };
function computeInd(bars){
  const close=bars.map(b=>b.c), vol=bars.map(b=>b.v);
  const ma={}; PRICE_MAS.forEach(n=>ma[n]=SMA(close,n));
  const mid=SMA(close,20), sd=STD(close,20,mid);
  const bu=mid.map((m,i)=>(m!=null&&sd[i]!=null)?m+2*sd[i]:null), bl=mid.map((m,i)=>(m!=null&&sd[i]!=null)?m-2*sd[i]:null);
  const vma={}; VOL_MAS.forEach(n=>vma[n]=SMA(vol,n));
  return { ma, boll:{u:bu,m:mid,l:bl}, vma, macd:MACD(close) };
}
function openChart(sid){
  const daily=HISTORY[sid]; if(!daily) return;
  const r=RESULTS.find(x=>x["代號"]===sid)||{};
  CH.sid=sid; CH.offset=0; CH.hover=null;
  document.getElementById("cvCode").textContent=sid; document.getElementById("cvName").textContent=r["名稱"]||"";
  const chg=num(r["漲跌%"]), cc=chg>0?UP:chg<0?DOWN:"var(--muted)";
  document.getElementById("cvChg").innerHTML=`<span style="color:${cc}">${r["收盤"]||""} (${chg>0?"+":""}${r["漲跌%"]||"0"}%)</span>`;
  document.querySelectorAll(".pbtn").forEach(b=>b.classList.toggle("on",b.dataset.p==="D"));
  setPeriod("D"); document.getElementById("cv").classList.add("open");
}
function closeChart(){ document.getElementById("cv").classList.remove("open"); }
function setPeriod(p){ CH.period=p; CH.offset=0; CH.hover=null; CH.bars=aggregate(HISTORY[CH.sid], p); CH.ind=computeInd(CH.bars);
  CH.count=Math.min(CH.bars.length, p==="D"?90:(p==="W"?80:60)); drawChart(); }
document.querySelectorAll(".pbtn").forEach(b=>b.addEventListener("click",()=>{document.querySelectorAll(".pbtn").forEach(x=>x.classList.remove("on")); b.classList.add("on"); setPeriod(b.dataset.p);}));
function visRange(){ const N=CH.bars.length, cnt=Math.min(CH.count,N); let end=N-CH.offset; if(end>N)end=N; let start=end-cnt; if(start<0)start=0; return {start,end}; }
const PADL=50, PADR=12, GAP=8, DATEH=20;
function layout(W,H){ const usable=H-DATEH, ph=Math.round(usable*0.56), vh=Math.round(usable*0.20);
  return { price:{y0:0,y1:ph}, vol:{y0:ph+GAP,y1:ph+GAP+vh}, macd:{y0:ph+GAP+vh+GAP,y1:usable}, dateY:usable }; }
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
    if(up) ctx.strokeRect(x-bodyW/2,top,bodyW,hgt); else ctx.fillRect(x-bodyW/2,top,bodyW,hgt); }
  ctx.lineWidth=1.4;
  PRICE_MAS.forEach(m=>{ ctx.strokeStyle=MACOLOR[m]; ctx.beginPath(); let st=false; for(let i=start;i<end;i++){ const v=CH.ind.ma[m][i]; if(v==null){st=false;continue;} const x=xOf(i),y=pY(v); if(!st){ctx.moveTo(x,y);st=true;} else ctx.lineTo(x,y);} ctx.stroke(); });
  const N=CH.bars.length;
  PRICE_MAS.forEach(m=>{ const ki=(N-1)-(m-1); if(ki<start||ki>=end)return; const x=xOf(ki), y=L.price.y1-3; ctx.fillStyle=MACOLOR[m]; ctx.beginPath(); ctx.moveTo(x,y-7); ctx.lineTo(x-4,y); ctx.lineTo(x+4,y); ctx.closePath(); ctx.fill(); });
  let vmax=0; for(let i=start;i<end;i++){ vmax=Math.max(vmax,CH.bars[i].v); VOL_MAS.forEach(m=>{const v=CH.ind.vma[m][i]; if(v!=null)vmax=Math.max(vmax,v);}); }
  vmax=vmax||1; const vY=(v)=> L.vol.y1 - v/vmax*(L.vol.y1-L.vol.y0-4);
  ctx.fillStyle="#5e6f86"; ctx.textAlign="right"; ctx.fillText(Math.round(vmax)+"張",PADL-6,L.vol.y0+8);
  for(let i=start;i<end;i++){ const b=CH.bars[i], x=xOf(i), up=b.c>=b.o; ctx.fillStyle=up?"rgba(255,77,79,.75)":"rgba(34,197,94,.75)"; const bodyW=Math.max(bw*0.6,1), y=vY(b.v); ctx.fillRect(x-bodyW/2,y,bodyW,L.vol.y1-y); }
  ctx.lineWidth=1.3; VOL_MAS.forEach(m=>{ ctx.strokeStyle=MACOLOR[m]; ctx.beginPath(); let st=false; for(let i=start;i<end;i++){ const v=CH.ind.vma[m][i]; if(v==null){st=false;continue;} const x=xOf(i),y=vY(v); if(!st){ctx.moveTo(x,y);st=true;} else ctx.lineTo(x,y);} ctx.stroke(); });
  const {dif,dea,osc}=CH.ind.macd; let mmax=1e-9; for(let i=start;i<end;i++){ [dif[i],dea[i],osc[i]].forEach(v=>{if(v!=null)mmax=Math.max(mmax,Math.abs(v));}); }
  const mMid=(L.macd.y0+L.macd.y1)/2, mH=(L.macd.y1-L.macd.y0-4)/2, mY=(v)=> mMid - v/mmax*mH;
  ctx.strokeStyle="rgba(255,255,255,0.12)"; ctx.beginPath(); ctx.moveTo(PADL,mMid); ctx.lineTo(W-PADR,mMid); ctx.stroke();
  ctx.fillStyle="#5e6f86"; ctx.textAlign="left"; ctx.fillText("MACD(12,26,9)",PADL+2,L.macd.y0+9);
  for(let i=start;i<end;i++){ const v=osc[i]; if(v==null)continue; const x=xOf(i), y=mY(v); ctx.fillStyle=v>=0?"rgba(255,77,79,.8)":"rgba(34,197,94,.8)"; const bodyW=Math.max(bw*0.5,1); ctx.fillRect(x-bodyW/2,Math.min(y,mMid),bodyW,Math.abs(y-mMid)||1); }
  const dl=(arr,col)=>{ ctx.strokeStyle=col; ctx.lineWidth=1.3; ctx.beginPath(); let st=false; for(let i=start;i<end;i++){ const v=arr[i]; if(v==null){st=false;continue;} const x=xOf(i),y=mY(v); if(!st){ctx.moveTo(x,y);st=true;} else ctx.lineTo(x,y);} ctx.stroke(); };
  dl(dif,"#e8c34a"); dl(dea,"#4d9fff");
  ctx.fillStyle="#5e6f86"; ctx.textAlign="center"; ctx.textBaseline="top";
  const ticks=Math.min(6,n); for(let t=0;t<ticks;t++){ const i=start+Math.floor((n-1)*t/(ticks-1||1)); ctx.fillText(CH.bars[i].d.slice(2),xOf(i),L.dateY+4); }
  if(idx>=start&&idx<end){ const x=xOf(idx); ctx.strokeStyle="rgba(255,255,255,0.28)"; ctx.setLineDash([4,3]); ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,L.dateY); ctx.stroke(); ctx.setLineDash([]); }
  updateReadout(idx);
}
function updateReadout(i){
  const b=CH.bars[i]; if(!b){document.getElementById("readout").innerHTML=""; return;}
  const prev=i>0?CH.bars[i-1].c:b.o, chg=((b.c-prev)/prev*100), cc=b.c>=prev?UP:DOWN;
  const it=(k,v,c)=>`<div class="it"><span class="k">${k}</span><span class="v" ${c?`style="color:${c}"`:""}>${v}</span></div>`;
  document.getElementById("readout").innerHTML=it("日期",b.d)+it("開",b.o.toFixed(2),b.o>=prev?UP:DOWN)+it("高",b.h.toFixed(2),UP)+it("低",b.l.toFixed(2),DOWN)+it("收",b.c.toFixed(2),cc)+it("漲跌",(chg>=0?"+":"")+chg.toFixed(2)+"%",cc)+it("量",Math.round(b.v).toLocaleString()+" 張");
}
const canvas=document.getElementById("chartCanvas");
function cx(clientX){ const r=canvas.getBoundingClientRect(); return clientX-r.left; }
function scrubAt(x){ const {start,end}=visRange(), n=end-start, bw=(canvas.parentElement.clientWidth-PADL-PADR)/n; let i=start+Math.floor((x-PADL)/bw); i=Math.max(start,Math.min(end-1,i)); CH.hover=i; drawChart(); }
canvas.addEventListener("mousemove",e=>{ if(!CH.bars.length)return; scrubAt(e.offsetX); });
canvas.addEventListener("mouseleave",()=>{ CH.hover=null; drawChart(); });
canvas.addEventListener("wheel",e=>{ if(!CH.bars.length)return; e.preventDefault(); const N=CH.bars.length, step=Math.max(2,Math.round(CH.count*0.12)); CH.count=Math.max(20,Math.min(N, CH.count+(e.deltaY>0?step:-step))); if(CH.offset>N-CH.count)CH.offset=Math.max(0,N-CH.count); CH.hover=null; drawChart(); },{passive:false});
let drag=null;
canvas.addEventListener("mousedown",e=>{ drag={x:e.clientX,off:CH.offset}; });
window.addEventListener("mouseup",()=>{ drag=null; });
window.addEventListener("mousemove",e=>{ if(!drag||!CH.bars.length)return; const bw=(canvas.parentElement.clientWidth-PADL-PADR)/Math.min(CH.count,CH.bars.length), dB=Math.round((e.clientX-drag.x)/bw), N=CH.bars.length; CH.offset=Math.max(0,Math.min(N-Math.min(CH.count,N), drag.off+dB)); drawChart(); });
let pinch=null;
function tdist(t){ return Math.hypot(t[0].clientX-t[1].clientX, t[0].clientY-t[1].clientY); }
function tmid(t){ return cx((t[0].clientX+t[1].clientX)/2); }
canvas.addEventListener("touchstart",e=>{ if(!CH.bars.length)return;
  if(e.touches.length===1){ scrubAt(cx(e.touches[0].clientX)); pinch=null; }
  else if(e.touches.length>=2){ pinch={d:tdist(e.touches),off:CH.offset,cnt:CH.count,mid:tmid(e.touches)}; CH.hover=null; } },{passive:false});
canvas.addEventListener("touchmove",e=>{ if(!CH.bars.length)return; e.preventDefault();
  if(e.touches.length===1&&!pinch){ scrubAt(cx(e.touches[0].clientX)); }
  else if(e.touches.length>=2&&pinch){ const nd=tdist(e.touches), ratio=pinch.d/(nd||1), N=CH.bars.length;
    CH.count=Math.max(20,Math.min(N,Math.round(pinch.cnt*ratio)));
    const bw=(canvas.parentElement.clientWidth-PADL-PADR)/Math.min(CH.count,N), dB=Math.round((tmid(e.touches)-pinch.mid)/bw);
    CH.offset=Math.max(0,Math.min(N-Math.min(CH.count,N), pinch.off+dB)); drawChart(); } },{passive:false});
canvas.addEventListener("touchend",e=>{ if(e.touches.length===0)pinch=null; },{passive:false});
window.addEventListener("resize",()=>{ if(document.getElementById("cv").classList.contains("open"))drawChart(); });
document.addEventListener("keydown",e=>{ if(e.key==="Escape")closeChart(); });

renderDD();
renderTrust();
render();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
