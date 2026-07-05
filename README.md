# twstk

stock-screener

每個交易日 17:30（台北）由 GitHub Actions 自動抓取 FinMind（Sponsor）等資料，
建置靜態網站發佈到 GitHub Pages。

## 頁面

| 頁面 | 產生器 | 內容 |
|---|---|---|
| `index.html` | `build_site.py` | 指數回撤・爆量起漲選股・投信連買・資金流向（熱圖＋120日輪動）・個股K線（表頭發行/流通張數；點按鎖定十字線＋副圖同步；副圖 量/投信/外資/400張大戶、下圖 MACD/RSI/KD/主力；主力與大戶歷史回補至 2019；概念股標籤） |
| `market.html` | `tw_market_analysis.py` | 台美股每日市場分析：總覽/盤勢研判/資金流向/族群雷達（漲跌主軸・底部起漲・持續強勢）/每日監控清單/風險儀表 |
| `chuzhi.html` | `tw_disposition.py` | 處置股專區（即將/確定/處置中/出關＋分點籌碼） |

## 資料流

`tw_volume_breakout_screener_v2.py`（FinMind → `twstock.db`＋`output/*.json`）
→ `build_site.py` → `tw_disposition.py` → `tw_industry_flow.py` → `tw_market_analysis.py`

美股/總經資料由 `tw_market_analysis.py` 透過 yfinance 抓取；本地離線預覽可用
`python tw_market_analysis.py --demo`。

個股K線副圖資料：三大法人（外資/投信/合計「主力」）近端由 TWSE T86 每日更新（上市），
更早的歷史則以 FinMind `TaiwanStockInstitutionalInvestorsBuySell` 深度回補至 `HISTORY_START`（預設 2019-01）；
「400張大戶持股%」來自 FinMind `TaiwanStockHoldingSharesPer`（集保股權分散，週更新，同樣回補至 2019，
存於 `shareholding` 表）。表頭「發行/流通張數」的發行股數來自 FinMind `TaiwanStockShareholding`
的 `NumberOfSharesIssued`（存於 `stockmeta` 表），流通張數 = 發行 ×(1−400張大戶%)（大戶已含董監＋法人大股東）。
深度回補以「快取 DB＋每次 run 上限」分批補齊；要一次補到位可手動觸發 `daily.yml` 的 `deep_backfill`。
概念股標籤由 `tw_concepts.py`（人工維護對照表）提供。
