# 部署到 VM（Hetzner / Ubuntu）

把 digest 排程從本機 Mac 搬到一台 24/7 常開的 VM，根治「Mac 睡著漏跑」，並為未來的 RAG/chatbot 準備好一個家。

---

## 1. 開一台 Hetzner VM
- 到 [console.hetzner.cloud](https://console.hetzner.cloud) → 建 Project → Add Server
- 機型 **CX22**（2 vCPU / 4GB，~€4/月）、映像 **Ubuntu 24.04**
- **SSH keys**：把你本機的公鑰貼上（`cat ~/.ssh/id_ed25519.pub`；沒有就 `ssh-keygen -t ed25519`）
- 建立後記下 **IP**，連線：`ssh root@<IP>`

## 2.（建議）建一個非 root 使用者
```bash
adduser --disabled-password --gecos "" xbot && usermod -aG sudo xbot
rsync --archive ~/.ssh /home/xbot/ && chown -R xbot:xbot /home/xbot/.ssh
su - xbot
```

## 3. Clone 專案 + 設定 git 身分
```bash
git clone https://github.com/11087tim/AlphaHelix_bot.git ~/Alphehelix_X_bot
cd ~/Alphehelix_X_bot
git config user.name "xbot" && git config user.email "bot@alphahelix"
```

## 4. 從本機 Mac 搬「私密檔 + 狀態」上來
這些是 gitignore、不在 repo 裡，要一次性搬上去。**在你的 Mac** 執行：
```bash
cd /Users/chenyanting/Alphehelix_X_bot
scp .env config.yaml graph.yaml reports_config.yaml \
    state.json digests.json memory.json snapshot.json podcast_seen.json \
    xbot@<IP>:~/Alphehelix_X_bot/
# 財報 brief（digest 會用到，PDF 可暫時不搬）：
rsync -av reports_data/analysis xbot@<IP>:~/Alphehelix_X_bot/reports_data/
```

## 5. 讓 VM 能把網站推回 GitHub Pages
VM 需要「寫入」權限才能 push docs。用 Deploy Key（最簡單、只綁這個 repo）：
```bash
# 在 VM 上：
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""   # 若還沒有
cat ~/.ssh/id_ed25519.pub
```
把印出的公鑰貼到 GitHub repo → **Settings → Deploy keys → Add deploy key**，**勾選 Allow write access**。
然後把 remote 改成 SSH：
```bash
cd ~/Alphehelix_X_bot
git remote set-url origin git@github.com:11087tim/AlphaHelix_bot.git
ssh -T git@github.com    # 首次確認指紋，輸入 yes
```

## 6. 一鍵部署
```bash
cd ~/Alphehelix_X_bot
bash deploy/setup.sh
```
會：裝 python/ffmpeg/git、設時區 Asia/Taipei、建 venv 裝套件、安裝並啟用 systemd timer（08:00/20:00 run、19:30 longform）。

## 7. 驗證
```bash
.venv/bin/python -m src.main run          # 手動跑一次確認端到端 OK
systemctl list-timers 'xbot-*'            # 看下次觸發時間
journalctl -u xbot-run.service -n 50      # 看執行 log
```

## 8. 收尾
- VM 跑穩後，**把本機 Mac 的 launchd 停掉**避免重複寄信：
  `launchctl unload ~/Library/LaunchAgents/com.alphehelix.xbot.*.plist`
- Mac 之後當「開發/備份」用（手動 `resynth`、財報 pipeline 等）。

---

## 常用維運
| 動作 | 指令（在 VM）|
|---|---|
| 手動正式補跑 | `XBOT_FORCE_PROD=1 .venv/bin/python -m src.main run` |
| 更新程式碼 | `git pull`（config/state 不受影響，都是 gitignore）|
| 看排程 | `systemctl list-timers 'xbot-*'` |
| 看某服務 log | `journalctl -u xbot-longform.service -n 100` |
| 暫停/恢復排程 | `sudo systemctl disable/enable --now xbot-run.timer` |

## 未來 RAG / chatbot
同一台 VM 之後可再開一個 FastAPI `/chat` 服務（重用現有 Python 檢索/LLM 程式碼），前面加 Caddy/Nginx 反向代理 + HTTPS，網站側欄 widget 打這個端點即可——不必再搬家。
