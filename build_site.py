# -*- coding: utf-8 -*-
r"""
雲端網頁版看板產生器（手機觸控 + 可加到主畫面當 App）
================================================================
給 GitHub Actions 在雲端執行：讀 output\breakout_*.csv + twstock.db，
產生 site\index.html（給 GitHub Pages 發布）與 site\manifest.json（PWA）。

與本機版差異：
  ‧ 輸出固定為 site/index.html（不是 dashboard_日期.html），方便 Pages 當首頁
  ‧ 不會自動開瀏覽器（雲端沒有瀏覽器）
  ‧ K線圖加上「手機觸控」：單指拖移看十字線讀數、雙指縮放/平移
  ‧ 加入 PWA 設定，手機可「加入主畫面」全螢幕當 App 用
  ‧ 顯示資料日與更新時間（台北時間）

需要套件：pandas
"""
import os
import sys
import glob
import json
import sqlite3
import datetime
import pandas as pd

DB_PATH = "twstock.db"
LOOKBACK_BARS = 1000
OUT_DIR = "site"


def find_latest_csv():
    cands = glob.glob(os.path.join("output", "breakout_*.csv")) or glob.glob("breakout_*.csv")
    return max(cands, key=os.path.getmtime) if cands else None


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


def build_html(results, history, date, count, db_ok, gentime):
    return (TEMPLATE
            .replace("/*__RESULTS__*/null", json.dumps(results, ensure_ascii=False))
            .replace("/*__HISTORY__*/null", json.dumps(history, ensure_ascii=False))
            .replace("/*__DBOK__*/false", "true" if db_ok else "false")
            .replace("__DATE__", date or "")
            .replace("__GENTIME__", gentime)
            .replace("__COUNT__", str(count)))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else find_latest_csv()
    if not path or not os.path.exists(path):
        print("找不到 CSV（output\\breakout_*.csv）。請先執行選股程式。")
        sys.exit(1)
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    results = df.to_dict(orient="records")
    if not results:
        # 沒有入選也要產生頁面，避免 Pages 壞掉
        results = []
    date = results[0].get("資料日", "") if results else ""
    ids = [r.get("代號", "") for r in results if r.get("代號")]
    history, db_ok = load_history(ids, DB_PATH)
    gentime = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_html(results, history, date, len(results), db_ok, gentime))
    with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({
            "name": "爆量起漲選股", "short_name": "爆量起漲",
            "display": "standalone", "orientation": "portrait",
            "background_color": "#0a0f1a", "theme_color": "#0a0f1a", "start_url": "."
        }, f, ensure_ascii=False)
    print(f"已產生 {OUT_DIR}/index.html（{len(results)} 檔，資料日 {date}，更新 {gentime}）")


# ============================================================
#  HTML（含手機觸控 + PWA）
#  台股慣例：紅漲綠跌。
# ============================================================
TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="爆量起漲">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0a0f1a">
<link rel="manifest" href="manifest.json">
<title>月均量爆量起漲 ・ __DATE__</title>
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
    -webkit-font-smoothing:antialiased; padding:16px 12px 36px;
    padding-top:calc(16px + env(safe-area-inset-top)); }
  .num{font-variant-numeric:tabular-nums;}
  .wrap{max-width:1180px; margin:0 auto;}
  header h1{font-size:19px; font-weight:800; margin:0;}
  header h1 .bolt{color:var(--amber);}
  .sub{font-size:12px; color:var(--muted); margin-top:4px;}
  .cards{display:grid; grid-template-columns:repeat(2,1fr); gap:9px; margin:14px 0;}
  .stat{background:var(--card); border:1px solid var(--border); border-radius:11px; padding:13px 15px;}
  .stat .l{font-size:11px; color:var(--dim); margin-bottom:5px;}
  .stat .v{font-size:24px; font-weight:800; line-height:1;}
  .stat .s{font-size:11px; color:var(--muted); margin-top:4px;}
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
  .foot{text-align:center; color:var(--dim); font-size:11px; margin-top:14px;}

  #cv{position:fixed; inset:0; background:#070b14; z-index:50; display:none; flex-direction:column;
      padding-top:env(safe-area-inset-top);}
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
  @media(min-width:640px){ .cards{grid-template-columns:repeat(4,1fr);} body{padding:22px 18px 40px;} }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1><span class="bolt">⚡</span> 月均量爆量起漲</h1>
    <div class="sub" id="subtitle">資料日 __DATE__ ・ 共 __COUNT__ 檔 ・ 更新 __GENTIME__</div>
  </header>
  <div class="cards" id="cards"></div>
  <div class="panel"><div class="ph">量比排行（前 20）</div><div class="bars" id="bars"></div></div>
  <div class="controls">
    <input id="q" placeholder="搜尋代號或名稱…" autocomplete="off">
    <button class="chip on" data-mkt="全部">全部</button>
    <button class="chip" data-mkt="上市">上市</button>
    <button class="chip" data-mkt="上櫃">上櫃</button>
    <span class="hint">點欄位排序 ・ 點名稱看K線</span>
  </div>
  <div class="tablewrap"><table><thead><tr id="thead"></tr></thead><tbody id="tbody"></tbody></table></div>
  <div class="foot">機械式初篩 ・ 進場前仍需看籌碼、消息面與基本面</div>
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
const DB_OK   = /*__DBOK__*/false;

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
  let d = RESULTS.filter(r=>r["代號"]);
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
    return `<div class="barrow"><div class="lbl">${r["代號"]} ${r["名稱"]||""}</div><div class="bartrack"><div class="barfill" style="width:${w}%;background:${c}"></div></div><div class="barval" style="color:${c}">${v.toFixed(2)}x</div></div>`;}).join("");
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
    const has=HISTORY[r["代號"]]?`onclick="openChart('${r["代號"]}')"`:'title="無歷史資料"';
    const tags=(r["強度標記"]||"").split("·").filter(Boolean).map(t=>{const c=TAGS[t]||["rgba(94,111,134,.15)","#93a3b8","rgba(94,111,134,.3)"]; return `<span class="tag" style="background:${c[0]};color:${c[1]};border-color:${c[2]}">${t}</span>`;}).join("");
    return `<tr><td class="l"><span class="code">${r["代號"]}</span></td><td class="l"><span class="nm" ${has}>${r["名稱"]||""}</span></td>
      <td><span class="mkt ${mkt}">${r["市場"]}</span></td><td class="num">${r["收盤"]}</td>
      <td class="num" style="color:${cc};font-weight:700">${chg>0?"+":""}${r["漲跌%"]}%${lim}</td>
      <td class="num">${fmtInt(r["成交量(張)"])}</td><td class="num" style="color:var(--muted)">${fmtInt(r["月均量(張)"])}</td>
      <td class="num"><span class="vr" style="color:${vc}">${r["量比"]}x</span></td><td class="num">${r["5日量/月量"]}</td>
      <td class="num">${r["季線乖離%"]}%</td>
      <td><span class="scorewrap"><span class="scoretrack"><span class="scorefill" style="width:${Math.min(sv,100)}%;background:${scC}"></span></span><span class="scoreval" style="color:${scC}">${r["評分"]}</span></span></td>
      <td class="tags">${tags}</td></tr>`;
  }).join("")||`<tr><td colspan="12" style="text-align:center;color:var(--dim);padding:36px">沒有符合條件的標的</td></tr>`;
}
function render(){const d=view(); renderCards(d); renderBars(d); renderHead(); renderTable(d);
  document.getElementById("subtitle").textContent=`資料日 __DATE__ ・ 顯示 ${d.length} 檔 ・ 更新 __GENTIME__`;}
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
function layout(W,H){ const usable=H-DATEH, ph=Math.round(usable*0.56), vh=Math.round(usable*0.20), mh=usable-ph-vh-2*GAP;
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
// 滑鼠
canvas.addEventListener("mousemove",e=>{ if(!CH.bars.length)return; scrubAt(e.offsetX); });
canvas.addEventListener("mouseleave",()=>{ CH.hover=null; drawChart(); });
canvas.addEventListener("wheel",e=>{ if(!CH.bars.length)return; e.preventDefault(); const N=CH.bars.length, step=Math.max(2,Math.round(CH.count*0.12)); CH.count=Math.max(20,Math.min(N, CH.count+(e.deltaY>0?step:-step))); if(CH.offset>N-CH.count)CH.offset=Math.max(0,N-CH.count); CH.hover=null; drawChart(); },{passive:false});
let drag=null;
canvas.addEventListener("mousedown",e=>{ drag={x:e.clientX,off:CH.offset}; });
window.addEventListener("mouseup",()=>{ drag=null; });
window.addEventListener("mousemove",e=>{ if(!drag||!CH.bars.length)return; const bw=(canvas.parentElement.clientWidth-PADL-PADR)/Math.min(CH.count,CH.bars.length), dB=Math.round((e.clientX-drag.x)/bw), N=CH.bars.length; CH.offset=Math.max(0,Math.min(N-Math.min(CH.count,N), drag.off+dB)); drawChart(); });
// 觸控
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
render();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
