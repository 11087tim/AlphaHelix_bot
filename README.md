# Alphehelix X Bot

定時抓取「指定 X 帳號的貼文」與「指定關鍵字/hashtag 的近期熱門推文」，用 LLM（透過 OpenRouter）做**跨作者觀點彙整與評斷**（不是逐則摘要，而是依主題比較各作者的異同、加入投資判斷），輸出成靜態網站（GitHub Pages）並寄送 Email。本 repo 另含一套獨立的**台股財報分析 pipeline**（`reports/`）。

## 常用 CLI 指令速查

先啟用虛擬環境：`cd ~/Alphehelix_X_bot && source .venv/bin/activate`

### X 觀點彙整 bot（`src.main`）
```bash
python -m src.main fetch        # 只收集新貼文到 pending（不做 LLM、不寄信）
python -m src.main synthesis    # 對累積貼文做跨作者彙整 → 網站 → 自動 push → 寄信（清空 pending 前先存 snapshot）
python -m src.main run          # 一次跑完 fetch + synthesis（排程用）
python -m src.main resynth      # 用 snapshot（或現有 pending）重跑彙整，只出本機預覽：不推送/不寄信/不清空。改 prompt 後免費看效果
python -m src.main render       # 只用既有 digests 重繪網站 + push（改樣板後用，不呼叫 LLM）
python -m src.main memory-backfill  # 從既有 digests.json 回填跨時間記憶帳本 memory.json（Sonnet 萃取立場，跳過已萃取者）
python -m src.main podcast       # 抓長訪談 podcast 新集 → Whisper 轉錄 → 蒸餾投資要點 → 加入 pending（需 .env 設 GROQ_API_KEY、系統裝 ffmpeg）
python -m src.main podcast-seed  # 把各 feed 目前集數設為基準（已讀），之後 podcast 只處理新發布的集；新增 feed 後想略過其舊集時也可用
python -m src.main youtube       # 抓 YouTube 頻道新片 → 免費字幕(youtube-transcript-api) → 蒸餾 → 加入 pending
python -m src.main youtube-seed  # 把各頻道目前影片設為基準，之後只處理新上片
python -m src.main longform      # Podcast + YouTube 一次跑完（每日 19:30 排程用）
```

### 台股財報分析 pipeline（`reports.main`）
先編 `reports_config.yaml`（股號、年、季、語言、模型）。
```bash
python -m reports.main fetch                # 平行下載 MOPS 財報 PDF（去重、可續跑）
python -m reports.main extract              # PDF → 純文字
python -m reports.main analyze 2330 2024 4  # 單季自適應分析 → 投資 brief
python -m reports.main aggregate 4927 8     # 近 8 季跨季趨勢彙集（平行分析、已分析的季跳過）
python -m reports.main eval 2330 2024 4     # A/B 保真度驗證（便宜模型擷取 vs 原文）
```
產出在 `reports_data/`：`raw/`（PDF）、`text/`（文字）、`analysis/`（分析 .md）。

### launchd 排程（X bot，每天 08:00 / 20:00）
```bash
tail -f xbot.log                                                  # 看執行紀錄
launchctl start com.alphehelix.xbot.daily                        # 立刻手動跑一次
launchctl unload ~/Library/LaunchAgents/com.alphehelix.xbot.daily.plist   # 停用
launchctl load  ~/Library/LaunchAgents/com.alphehelix.xbot.daily.plist    # 啟用
launchctl list | grep alphehelix                                 # 確認是否載入
```

### git（若自動 push 失敗想手動推）
```bash
git add docs/ && git commit -m "update" && git push
```

## 運作方式

> 目前安裝的排程是**每天 2 次（08:00 / 20:00）的 `run` 模式**（一次跑完 fetch + synthesis，見上方指令速查）。以下說明底層的兩個獨立工作，`fetch` 與 `synthesis` 也可單獨使用。

分成「抓取累積」與「彙整」兩個獨立工作：

| 模式 | 指令 | 頻率 | 做什麼 |
|---|---|---|---|
| **fetch** | `python -m src.main fetch` | 每小時（24 次/天）| 抓「過去 1 小時內」各追蹤來源的新貼文 → 去重 → 累積到 `pending.json`（**不做 LLM、不更新網站、不寄信**，很便宜）|
| **synthesis** | `python -m src.main synthesis` | 每天 3 次（08:00 / 15:00 / 20:30）| 把「上次彙整至今」累積的所有作者貼文做**跨作者觀點彙整**（依主題比較異同、加評斷）→ 更新網站 → 自動 push → 寄信 → 清空累積 |

```
每小時 fetch:      抓取(X API, 1hr窗) → 去重 → 累積到 pending.json
每天 3 次 synthesis: 取出 pending 全部貼文 → 跨作者觀點彙整(含 [n] 引用/圖片描述)
                    → 產生網站(docs/) → 自動 push → 寄信(Gmail) → 清空 pending
```

- **為什麼分兩段**：跨作者比較需要「多位作者對同一主題的發言」才有料，1 小時內通常湊不齊，所以每小時只負責「便宜地收集」，真正的 LLM 分析集中在每天 3 次的彙整。
- **彙整格式**：依主題組織，比較各作者「同意什麼、不同意什麼」，並加入評斷；句中用 `[1]`、`[2]` 引用標記連到對應推文，內文不放裸網址。
- **版面**：網站每份彙整是可折疊區塊（最新展開、舊的收合）；信件因 email 軟體不支援折疊，改為攤平呈現。
- **彙整規則可自訂**：改 `config.yaml` 的 `openrouter.system_prompt` 即可調整分析行為（留空則用內建預設）。

## 一、安裝

建議使用虛擬環境：

```bash
cd /Users/chenyanting/Alphehelix_X_bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 二、設定金鑰（.env）

複製範本並填入你的金鑰：

```bash
cp .env.example .env
```

- **X_BEARER_TOKEN**：到 [X Developer Portal](https://developer.x.com/en/portal/products) 訂閱 **Basic 方案（$200/月起）**，建立 App 後取得 Bearer Token。免費方案無法讀取/搜尋推文。
- **OPENROUTER_API_KEY**：到 [OpenRouter](https://openrouter.ai/keys) 註冊並建立 API key。
- **GMAIL_ADDRESS / GMAIL_APP_PASSWORD**：Gmail 帳號需先開啟兩步驟驗證，再到 [App Passwords](https://myaccount.google.com/apppasswords) 產生一組 16 碼應用程式密碼（不是你的登入密碼）。

## 三、編輯 config.yaml

先從範本複製一份（`config.yaml` 含個人 email 與追蹤清單，已列入 `.gitignore` 不會上傳）：

```bash
cp config.example.yaml config.yaml
```

再依需求編輯：

```yaml
accounts: ["elonmusk", "OpenAI"]   # 要追蹤的帳號（不含 @）
keywords: ["#AI", "台積電"]         # 要追蹤的關鍵字或 hashtag
max_results_per_source: 10
fetch_window_hours: 1              # 只抓過去幾小時內的貼文
openrouter:
  model: "anthropic/claude-haiku-4.5"   # 可隨時替換成 OpenRouter 上任何模型
  system_prompt: |                       # 摘要規則/風格，可自由編輯；留空則用內建預設
    你是一個推文摘要助手…（改這裡即可調整摘要行為，改完下次執行生效）
site:
  title: "我的 X 摘要"
  output_dir: "docs"
  url: "https://<your-account>.github.io/<repo>/"   # 信件底部連回網站用
email:
  to:
    - "you@example.com"           # 可填多個收件人
  subject_prefix: "[X Digest]"
```

## 四、手動測試執行

```bash
source .venv/bin/activate
python -m src.main fetch       # 抓取新貼文累積到 pending（不做 LLM、不更新網站、不寄信）
python -m src.main synthesis   # 對累積貼文做跨作者彙整 → 更新網站 → 自動 push → 寄信 → 清空 pending
```

`fetch` 若沒有新貼文，pending 不變。`synthesis` 若沒有累積貼文，不會產生彙整也不寄信。想單獨測彙整，可先跑幾次 `fetch` 再跑一次 `synthesis`。

## 五、安裝排程（launchd）

需要安裝**兩個** LaunchAgent：`fetch`（每小時）與 `synthesis`（每天三次）。

1. 分別編輯 `scripts/com.alphehelix.xbot.fetch.plist` 與 `scripts/com.alphehelix.xbot.synthesis.plist`，把 `__PYTHON__` 換成 venv 內 python 絕對路徑（例如 `/Users/chenyanting/Alphehelix_X_bot/.venv/bin/python`），`__PROJECT_DIR__` 換成專案絕對路徑（`/Users/chenyanting/Alphehelix_X_bot`）。
2. 安裝並載入：
   ```bash
   cp scripts/com.alphehelix.xbot.fetch.plist ~/Library/LaunchAgents/
   cp scripts/com.alphehelix.xbot.synthesis.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.alphehelix.xbot.fetch.plist
   launchctl load ~/Library/LaunchAgents/com.alphehelix.xbot.synthesis.plist
   ```
3. 排程時間依 Mac 系統本地時區，請確認系統時區為 **Asia/Taipei**。
4. 移除排程：對兩個檔案分別 `launchctl unload ~/Library/LaunchAgents/<檔名>`。

執行紀錄會寫入專案內的 `xbot.log`。

## 六、發布到 GitHub Pages

```bash
git add .
git commit -m "initial"
git remote add origin git@github.com:<your-account>/<repo>.git
git push -u origin main
```

到 GitHub repo 的 **Settings → Pages**，Source 選 `main` branch、資料夾選 `/docs`，即可用 `https://<your-account>.github.io/<repo>/` 瀏覽最新摘要。

**自動更新網站**：`config.yaml` 的 `site.auto_push: true` 開啟後，每次 `fetch` 產生網站後會自動 `git add docs/ && git commit && git push`，GitHub Pages 隨即自動重新部署，線上網站便自動更新（無變動時不會 push，不產生空 commit；push 失敗只記錄、不影響抓取）。前提是本機已設定好對 GitHub 的 push 權限（SSH 金鑰或憑證）。若想關閉改回手動 push，把 `auto_push` 設為 `false`。

## 專案結構

```
├── config.yaml               # 帳號 / 關鍵字 / 模型 / prompt 等設定（不進版控）
├── config.example.yaml       # 設定範本（進版控）
├── .env                      # 金鑰（不進版控）
├── pending.json              # 已抓取、尚未彙整的原始貼文（不進版控）
├── digests.json              # 每份彙整結果（供網站顯示，不進版控）
├── state.json                # 已處理推文 id + 帳號 ID 快取（不進版控）
├── src/
│   ├── config.py             # 讀取設定與環境變數
│   ├── x_client.py           # X API v2 封裝（時間窗 + 媒體）
│   ├── storage.py            # 已處理推文 id 與帳號 ID 快取
│   ├── pending_store.py      # 待彙整原始貼文暫存
│   ├── digest_store.py       # 彙整結果儲存（供網站歷史）
│   ├── summarizer.py         # OpenRouter 跨作者彙整（含 [n] 引用 / 視覺描述）
│   ├── site_generator.py     # 產生網站（折疊）與 email HTML（攤平）
│   ├── emailer.py            # Gmail SMTP 寄信
│   ├── publisher.py          # 自動 commit & push docs/
│   └── main.py               # 主流程（fetch / synthesis 兩模式）
├── templates/
│   ├── _macros.html          # 共用區塊樣板
│   ├── site.html             # 網站（可折疊彙整）
│   └── email.html            # 信件（攤平）
├── docs/                     # GitHub Pages 輸出
└── scripts/                  # 兩個 launchd 排程範本（fetch / synthesis）
```

## 注意事項

- X API 按量計費，請依帳號/關鍵字數量與抓取量調整 `max_results_per_source`、`fetch_window_hours` 與排程頻率。
- 視覺描述（`media.describe`）會依圖片數增加成本；`MAX_IMAGES_PER_GROUP` 控制單次上限。
- `.env`、`config.yaml`、`state.json`、`digests.json`、`pending.json` 已列入 `.gitignore`，不會被 push，避免金鑰與個資外洩。
- 網站每天在 `synthesis` 時更新並自動 push（`site.auto_push: true`）。
