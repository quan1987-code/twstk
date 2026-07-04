# twstk

stock-screener

每個交易日 17:30（台北）由 GitHub Actions 自動抓取 FinMind（Sponsor）等資料，
建置靜態網站發佈到 GitHub Pages。

## 頁面

| 頁面 | 產生器 | 內容 |
|---|---|---|
| `index.html` | `build_site.py` | 指數回撤・爆量起漲選股・投信連買・資金流向（熱圖＋120日輪動）・個股K線 |
| `market.html` | `tw_market_analysis.py` | 台美股每日市場分析：總覽/盤勢研判/資金流向/族群雷達（漲跌主軸・底部起漲・持續強勢）/每日監控清單/風險儀表 |
| `chuzhi.html` | `tw_disposition.py` | 處置股專區（即將/確定/處置中/出關＋分點籌碼） |

## 資料流

`tw_volume_breakout_screener_v2.py`（FinMind → `twstock.db`＋`output/*.json`）
→ `build_site.py` → `tw_disposition.py` → `tw_industry_flow.py` → `tw_market_analysis.py`

美股/總經資料由 `tw_market_analysis.py` 透過 yfinance 抓取；本地離線預覽可用
`python tw_market_analysis.py --demo`。
