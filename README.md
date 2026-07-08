# Alphehelix X Bot

定時抓取「指定 X 帳號的貼文」與「指定關鍵字/hashtag 的近期熱門推文」，用 LLM（透過 OpenRouter）摘要後，輸出成靜態網站（GitHub Pages）並寄送 Email。

## 運作方式（兩個排程）

分成「抓取」與「寄信」兩個獨立工作：

| 模式 | 指令 | 頻率 | 做什麼 |
|---|---|---|---|
| **fetch** | `python -m src.main fetch` | 每小時（24 次/天）| 抓「過去 1 小時內」的貼文 → LLM 摘要成「這一時段的事件摘要」→ 存進 `digests.json` → **更新網站**（不寄信）|
| **email** | `python -m src.main email` | 每天 3 次（08:00 / 15:00 / 20:30）| 把「上次寄信後累積、尚未寄出的時段摘要」合併成一封信寄出 |

```
每小時 fetch: 抓取(X API, 1hr窗) → 去重 → LLM 摘要(含 [n] 引用) → 存 digests.json → 產生網站(docs/)
每天 3 次 email: 撈未寄時段 → 合併 → 寄信(Gmail SMTP) → 標記已寄
```

- **時間窗（1hr）**：由 `config.yaml` 的 `fetch_window_hours` 控制。因為每小時抓一次、窗也是 1 小時，剛好無縫銜接、不漏文。
- **摘要格式**：每個時段整理成一段連貫敘述，句中用 `[1]`、`[2]` 引用標記連到對應推文，內文不放裸網址。
- **版面**：網站每個時段是可折疊區塊（最新展開、舊的收合）；信件因 email 軟體不支援折疊，改為攤平按時段分段。

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
python -m src.main fetch    # 抓取 + 摘要 + 更新網站（不寄信）
python -m src.main email    # 把尚未寄出的時段摘要合併寄信
```

`fetch` 若這一小時沒有新推文，不會建立時段、不更新網站。`email` 若沒有待寄的時段，不會寄空信。

## 五、安裝排程（launchd）

需要安裝**兩個** LaunchAgent：`fetch`（每小時）與 `email`（每天三次）。

1. 分別編輯 `scripts/com.alphehelix.xbot.fetch.plist` 與 `scripts/com.alphehelix.xbot.email.plist`，把 `__PYTHON__` 換成 venv 內 python 絕對路徑（例如 `/Users/chenyanting/Alphehelix_X_bot/.venv/bin/python`），`__PROJECT_DIR__` 換成專案絕對路徑（`/Users/chenyanting/Alphehelix_X_bot`）。
2. 安裝並載入：
   ```bash
   cp scripts/com.alphehelix.xbot.fetch.plist ~/Library/LaunchAgents/
   cp scripts/com.alphehelix.xbot.email.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.alphehelix.xbot.fetch.plist
   launchctl load ~/Library/LaunchAgents/com.alphehelix.xbot.email.plist
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
├── config.yaml               # 帳號 / 關鍵字 / 模型等設定（不進版控）
├── config.example.yaml       # 設定範本（進版控）
├── .env                      # 金鑰（不進版控）
├── digests.json              # 每小時摘要儲存 + 已寄信標記（不進版控）
├── state.json                # 已處理推文 id（不進版控）
├── src/
│   ├── config.py             # 讀取設定與環境變數
│   ├── x_client.py           # X API v2 封裝（含時間窗）
│   ├── storage.py            # 記錄已處理推文 id
│   ├── digest_store.py       # 每小時摘要儲存與待寄追蹤
│   ├── summarizer.py         # OpenRouter 摘要（含 [n] 引用）
│   ├── site_generator.py     # 產生網站（折疊）與 email HTML（攤平）
│   ├── emailer.py            # Gmail SMTP 寄信
│   └── main.py               # 主流程（fetch / email 兩模式）
├── templates/
│   ├── _macros.html          # 共用區塊樣板
│   ├── site.html             # 網站（可折疊時段）
│   └── email.html            # 信件（攤平時段）
├── docs/                     # GitHub Pages 輸出
└── scripts/                  # 兩個 launchd 排程範本（fetch / email）
```

## 注意事項

- X API 按量計費，請依帳號/關鍵字數量與抓取量調整 `max_results_per_source`、`fetch_window_hours` 與排程頻率。
- `.env`、`config.yaml`、`state.json`、`digests.json` 已列入 `.gitignore`，不會被 push，避免金鑰與個資外洩。
- 網站要更新到線上，需在 `fetch` 後把 `docs/` 變動 commit & push（可另外加自動 push）。
