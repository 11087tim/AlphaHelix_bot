# 設計草案：美股財務數據源（X × 財報跨源印證 v2）

> 狀態：規劃中（尚未實作）。v1 已完成台股（MOPS → `reports/` brief → `src/reports_bridge.py` 注入 🤖 延伸推論）。
> 本文規劃如何把同樣的跨源印證擴到美股持股：NVDA、AMD、MU、AVGO、GOOG、AMZN、MSFT、GLW、VIAV、SNDK。

## 為什麼是獨立工程
台股走 MOPS（`doc.twse.com.tw`）；美股沒有 MOPS。X 上被討論最多的正是美股，所以覆蓋美股才是這條線的主要價值，但需要另接資料源與另一套實體對應。

## 資料源選項

| 選項 | 內容 | 優點 | 缺點 |
|---|---|---|---|
| **SEC EDGAR**（官方免費）| 10-Q/10-K 全文 + XBRL `companyfacts` API（財務概念 JSON）| 免費、無金鑰、權威、含歷史逐季 | XBRL 概念需對應；敘事（MD&A/風險因子）在 HTML，需像 MOPS 那樣抽取 |
| 第三方 API（FMP / Finnhub / Alpha Vantage / Polygon）| 已解析好的損益/資產/現金流 + 比率 | JSON 好用、上手快 | 免費層有額度/延遲；敘事少；長期可能要付費 |
| 公司 IR / 財報新聞稿 | 當季關鍵數字 + **財測 guidance** | 最貼近 X 討論（guidance 是盤前重點）| 非結構化、每家格式不同 |

## 建議：以 SEC EDGAR 為主，分兩階段

實體對應：graph.yaml 的美股公司節點加一個 `cik`（SEC Central Index Key），與台股的 `report_code` 平行。`reports_bridge` 已經以 graph 為錨，只需加一條「美股後端」。

- **Phase 1 — 數字卡（小）**：打 EDGAR `companyfacts`（`https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json`），抽最新季的 Revenue / Gross margin / Operating income / EPS，附 YoY、QoQ 趨勢，做成「財報數字卡」。格式沿用現有事實卡（`一句話/重點/🚩` → 這裡是 `關鍵數字/趨勢`），**summarizer 的 prompt 不用改**。
- **Phase 2 — 敘事紅旗（大）**：抓 10-Q HTML，比照 `reports/analyze.py`（router → 抽取 → Opus 判讀）產出紅旗/盲點卡，補足只有數字看不到的東西。

## 架構落點
- 沿用 `reports_bridge.load_report_cards(tweets)`：內部依 graph 節點欄位分流——有 `report_code` 走台股 brief（現況），有 `cik` 走美股 EDGAR。
- 卡片格式維持一致 → digest 呈現、prompt 規則都不動，只是多了美股來源。
- 快取：EDGAR 有 fair-use 限速（需帶 User-Agent、約 10 req/s），把 `companyfacts` 落地成本地 JSON，逐季更新即可。

## 待決定
1. Phase 1 只用 EDGAR 數字，還是也接一個第三方 API 補 **guidance/consensus**（X 最愛討論「beat/miss 財測」，但 EDGAR 沒有 guidance）？
2. 敘事紅旗（Phase 2）是否值得——對美股大型權值股，紅旗較少、賣方覆蓋多，邊際價值可能不如台股中小型。
3. 更新節奏：綁定各公司財報日（earnings calendar）觸發，而非固定排程。

## 對應目標
主要服務**目標 3（獨特見解）**：讓 NVDA/MU 等美股的 X 即時敘事，也能和權威財報數字對照（印證/打臉/補盲點），而不只有台積電。
