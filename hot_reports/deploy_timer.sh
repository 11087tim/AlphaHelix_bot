#!/usr/bin/env bash
# 在 VM 上安裝 hot_reports 的 systemd service + timer（每天 23:00 Asia/Taipei）。
# 用法：在 VM 的專案目錄內執行  bash hot_reports/deploy_timer.sh
# 前置：deploy/setup.sh 已跑過（venv 存在）；另需 playwright chromium：
#   .venv/bin/pip install playwright && .venv/bin/python -m playwright install --with-deps chromium
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
cd "$PROJECT_DIR"

sudo tee /etc/systemd/system/hot-reports.service >/dev/null <<UNIT
[Unit]
Description=Hot foreign reports pipeline (valuelist -> nash-ai -> LLM digest)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
ExecStartPre=-/usr/bin/git pull --rebase --autostash --quiet
ExecStart=$PROJECT_DIR/.venv/bin/python -m hot_reports.main run
StandardOutput=append:$PROJECT_DIR/hot_reports.log
StandardError=append:$PROJECT_DIR/hot_reports.log
UNIT

sudo tee /etc/systemd/system/hot-reports.timer >/dev/null <<UNIT
[Unit]
Description=Run hot-reports daily at 23:00

[Timer]
OnCalendar=*-*-* 23:00:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now hot-reports.timer
echo "完成。檢查： systemctl list-timers hot-reports.timer ；手動跑： sudo systemctl start hot-reports.service ；看 log： tail -f $PROJECT_DIR/hot_reports.log"
