# twstk

stock-screener

每個交易日 17:30（台北）由 GitHub Actions 自動抓取**證交所／櫃買中心／集保結算所等官方免費開放資料**，
建置靜態網站發佈到 GitHub Pages。**不需要任何 API token。**

## 頁面

| 頁面 | 產生器 | 內容 |
|---|---|---|
| `index.html` | `build_site.py` | 指數回撤・爆量起漲選股・投信連買・資金流向（熱圖＋120日輪動）・個股K線（表頭發行/流通張數；點按鎖定十字線＋副圖同步；副圖 量/投信/外資/400張大戶、下圖 MACD/RSI/KD/主力；主力與大戶歷史回補至 2019；概念股標籤） |
| `market.html` | `tw_market_analysis.py` | 台美股每日市場分析：總覽/盤勢研判/資金流向/族群雷達（漲跌主軸・底部起漲・持續強勢）/每日監控清單/風險儀表 |
| `chuzhi.html` | `tw_disposition.py` | 處置股專區（即將/確定/處置中/出關＋籌碼集中度） |

## 資料流

`tw_volume_breakout_screener_v2.py`（官方免費來源 → `twstock.db`＋`output/*.json`）
→ `build_site.py` → `tw_disposition.py` → `tw_industry_flow.py` → `tw_market_analysis.py`

美股/總經資料由 `tw_market_analysis.py` 透過 yfinance 抓取；本地離線預覽可用
`python tw_market_analysis.py --demo`。

## 資料來源（全部免費、官方、免 token）

所有對外抓取集中在 `tw_free_sources.py`，其餘程式只呼叫它。

| 資料 | 來源 |
|---|---|
| 當日全市場價量 | 證交所 `exchangeReport/STOCK_DAY_ALL`、櫃買 `tpex_mainboard_daily_close_quotes` |
| 歷史價量（補洞／回補） | 證交所 `MI_INDEX`（全市場單日）、櫃買上櫃行情；新上市個股用 `STOCK_DAY`／櫃買個股月線（逐檔單月） |
| 三大法人買賣超（外資/投信/自營/合計） | 證交所 `T86`（上市）＋ 櫃買三大法人日報（上櫃），皆為「全市場單日」；深度歷史以同一路徑逐日回補至 `INST_HISTORY_START`（預設 2020-01） |
| 400 張大戶持股% | 集保結算所 TDCC 開放資料 `getOD.aspx?id=1-5`（持股分級 12~15 佔比加總，週更新，存於 `shareholding` 表） |
| 發行張數 | TDCC 合計級距股數（集保總股數）；缺漏者退回證交所／櫃買公司基本資料 `t187ap03`（存於 `stockmeta` 表） |
| 產業別／股名／市場別 | 證交所 `t187ap03_L`、櫃買 `mopsfin_t187ap03_O`；退回證交所 ISIN 服務 |
| 加權指數 TAIEX | 證交所 `MI_5MINS_HIST`（發行量加權股價指數歷史，日 OHLC） |
| 處置股名單 | 證交所 `announcement/punish` ＋ 櫃買 `tpex_disposal_information`（即時公告）；歷史期別由本地 `disposition` 表逐日累積 |
| 美股／總經 | Yahoo Finance（yfinance）、Stooq |

流通張數 = 發行 ×(1−400張大戶%)（大戶已含董監＋法人大股東），於網頁端計算。
概念股標籤由 `tw_concepts.py`（人工維護對照表）提供。

深度回補以「快取 DB＋每次 run 上限」分批補齊；要一次補多一點可手動觸發 `daily.yml` 的 `deep_backfill`
（官方端點有流量限制，證交所約 3 次/5 秒，上限不宜無限拉高）。

### 兩個資料取得方式改變的地方

* **券商分點**：處置股頁的「主5／主10」原本以券商分點資料計算。官方端唯一的分點來源是證交所
  BSR 網頁（需輸入驗證碼），沒有可自動化的免費 API，因此改用同樣代表大戶動向、且完全免費的
  **三大法人合計買賣超 ÷ 區間成交量 ×100** 作為籌碼集中度指標。
* **400 張大戶歷史**：TDCC 開放資料只提供最新一週，沒有免費的全市場歷史端點。DB 內既有的
  歷史會保留，之後每週自動累積一筆，時間拉長即恢復完整曲線。
