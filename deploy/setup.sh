#!/usr/bin/env bash
# Alphehelix X bot — VM 一鍵部署（Ubuntu / Debian，適用 Hetzner）
# 用法：在「已 clone 的專案目錄內」執行：  bash deploy/setup.sh
# 前置：先把私密檔放進專案根目錄（見 deploy/README.md）：
#   .env、config.yaml、graph.yaml、reports_config.yaml、以及要沿用的狀態檔（digests.json 等）
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
PY="$PROJECT_DIR/.venv/bin/python"
cd "$PROJECT_DIR"

echo "==> 專案目錄：$PROJECT_DIR（執行者：$RUN_USER）"

echo "==> [1/5] 安裝系統套件（python venv、ffmpeg、git）"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip ffmpeg git

echo "==> [2/5] 設定時區為 Asia/Taipei（systemd OnCalendar 會依此判定 08:00/19:30/20:00）"
sudo timedatectl set-timezone Asia/Taipei

echo "==> [3/5] 建立 venv 並安裝相依套件"
[ -d .venv ] || python3 -m venv .venv
"$PY" -m pip install --upgrade pip
"$PY" -m pip install -r requirements.txt

echo "==> [4/5] 檢查私密檔是否就位"
missing=0
for f in .env config.yaml graph.yaml; do
  [ -f "$f" ] || { echo "   ⚠ 缺少 $f（請依 README 從本機搬上來）"; missing=1; }
done
[ "$missing" = 0 ] && echo "   ✓ 必要私密檔都在" || echo "   ↑ 補齊後再啟用 timer（見最後說明）"

echo "==> [5/5] 產生並安裝 systemd service + timer"
# --- run：每天 08:00 與 20:00（fetch + 合成 + 推送 + 寄信）---
sudo tee /etc/systemd/system/xbot-run.service >/dev/null <<UNIT
[Unit]
Description=Alphehelix X bot - daily run (fetch + synthesis)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
ExecStartPre=-/usr/bin/git pull --rebase --autostash --quiet
ExecStart=$PY -m src.main run
UNIT

sudo tee /etc/systemd/system/xbot-run.timer >/dev/null <<'UNIT'
[Unit]
Description=Run Alphehelix digest at 08:00 and 20:00 (Asia/Taipei)

[Timer]
OnCalendar=*-*-* 08:00:00
OnCalendar=*-*-* 20:00:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT

# --- longform：每天 19:30（Podcast + YouTube 蒸餾進 pending）---
sudo tee /etc/systemd/system/xbot-longform.service >/dev/null <<UNIT
[Unit]
Description=Alphehelix X bot - longform (podcast + youtube)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
ExecStartPre=-/usr/bin/git pull --rebase --autostash --quiet
ExecStart=$PY -m src.main longform
UNIT

sudo tee /etc/systemd/system/xbot-longform.timer >/dev/null <<'UNIT'
[Unit]
Description=Run Alphehelix longform at 19:30 (Asia/Taipei)

[Timer]
OnCalendar=*-*-* 19:30:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT

# --- leverage：每交易日晚間 21:30（增量抓台股融資融券/不限用途 → 重建槓桿儀表板 → push）---
# 卡在 FinMind 週一~五 21:00 更新當日融資融券之後，抓到當天剛收盤的最新資料。
sudo tee /etc/systemd/system/xbot-leverage.service >/dev/null <<UNIT
[Unit]
Description=Alphehelix X bot - Taiwan leverage dashboard (margin/short/buxian)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
ExecStartPre=-/usr/bin/git pull --rebase --autostash --quiet
ExecStart=$PY -m src.main leverage
UNIT

sudo tee /etc/systemd/system/xbot-leverage.timer >/dev/null <<'UNIT'
[Unit]
Description=Run Alphehelix leverage dashboard at 21:30 on weekdays (Asia/Taipei)

[Timer]
OnCalendar=Mon..Fri *-*-* 21:30:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now xbot-run.timer xbot-longform.timer xbot-leverage.timer

echo
echo "✅ 完成。已啟用排程："
systemctl list-timers 'xbot-*' --no-pager || true
echo
echo "手動測試一次： $PY -m src.main run"
echo "看下次觸發時間： systemctl list-timers 'xbot-*'"
echo "看某次執行 log： journalctl -u xbot-run.service -n 50 --no-pager"
