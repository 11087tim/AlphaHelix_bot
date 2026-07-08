# Alphehelix X Bot

定時抓取「指定 X 帳號的貼文」與「指定關鍵字/hashtag 的近期熱門推文」，用 LLM（透過 OpenRouter）摘要後，同時輸出成靜態網站（GitHub Pages）並寄送 Email。每天執行三次（台灣時間 08:00 / 15:00 / 20:30），由 macOS launchd 排程觸發。

## 功能流程

```
抓取(X API v2) → 過濾已處理推文 → LLM 摘要(OpenRouter) → 產生網站(docs/) → 寄信(Gmail SMTP)
```

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
openrouter:
  model: "anthropic/claude-3.5-haiku"   # 可隨時替換成 OpenRouter 上任何模型
site:
  title: "我的 X 摘要"
  output_dir: "docs"
email:
  to: "yanting614@gmail.com"
  subject_prefix: "[X Digest]"
```

## 四、手動測試執行

```bash
source .venv/bin/activate
python -m src.main
```

執行後會：更新 `docs/index.html`、在 `docs/archive/` 新增一份存檔、並寄一封摘要信。若沒有新推文則不產出、不寄信。

## 五、安裝排程（launchd）

1. 編輯 `scripts/com.alphehelix.xbot.plist`，把 `__PYTHON__` 換成 venv 內的 python 絕對路徑（例如 `/Users/chenyanting/Alphehelix_X_bot/.venv/bin/python`），`__PROJECT_DIR__` 換成專案絕對路徑（`/Users/chenyanting/Alphehelix_X_bot`）。
2. 安裝並載入：
   ```bash
   cp scripts/com.alphehelix.xbot.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.alphehelix.xbot.plist
   ```
3. 排程時間依 Mac 系統本地時區，請確認系統時區為 **Asia/Taipei**。
4. 移除排程：`launchctl unload ~/Library/LaunchAgents/com.alphehelix.xbot.plist`

執行紀錄會寫入專案內的 `xbot.log`。

## 六、發布到 GitHub Pages

```bash
git add .
git commit -m "initial"
git remote add origin git@github.com:<your-account>/<repo>.git
git push -u origin main
```

到 GitHub repo 的 **Settings → Pages**，Source 選 `main` branch、資料夾選 `/docs`，即可用 `https://<your-account>.github.io/<repo>/` 瀏覽最新摘要。之後每次排程執行後，把更新後的 `docs/` commit & push 就會自動更新網站（可在 launchd 流程外自行加上 git 自動 push，或手動處理）。

## 專案結構

```
├── config.yaml           # 帳號 / 關鍵字 / 模型等設定
├── .env                  # 金鑰（不進版控）
├── src/
│   ├── config.py         # 讀取設定與環境變數
│   ├── x_client.py       # X API v2 封裝
│   ├── storage.py        # 記錄已處理推文 id（state.json）
│   ├── summarizer.py     # OpenRouter 摘要
│   ├── site_generator.py # 產生靜態網站
│   ├── emailer.py        # Gmail SMTP 寄信
│   └── main.py           # 主流程
├── templates/digest.html # 網站樣板
├── docs/                 # GitHub Pages 輸出
└── scripts/              # launchd 排程範本
```

## 注意事項

- X API Basic 方案有月配額與 rate limit，請依帳號/關鍵字數量與抓取量調整 `max_results_per_source` 與排程頻率。
- `.env` 與 `state.json` 已列入 `.gitignore`，不會被 push，避免金鑰外洩。
