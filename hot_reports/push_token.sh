#!/usr/bin/env bash
# 更新 VM 上的 nash-ai token（微信掃碼 token 有效期 24h，失效時 email 會提醒）。
# 用法（在 Mac 上）：
#   1. 瀏覽器登入 https://www.nash-ai.cn/login.html（微信掃碼）
#   2. 開發者工具 Console 執行： localStorage.getItem('token')   複製字串（不含引號）
#   3. bash hot_reports/push_token.sh <token字串>
set -euo pipefail
VM_HOST="${VM_HOST:-alphahelix_vm}"
VM_PATH="~/Alphehelix_X_bot/hot_reports_data/token.txt"

if [ $# -lt 1 ]; then echo "用法: bash hot_reports/push_token.sh <token>"; exit 1; fi
ssh "$VM_HOST" "mkdir -p ~/Alphehelix_X_bot/hot_reports_data"
printf '%s' "$1" | ssh "$VM_HOST" "cat > $VM_PATH"
echo "已更新 $VM_HOST:$VM_PATH"
ssh "$VM_HOST" "cd ~/Alphehelix_X_bot && .venv/bin/python -m hot_reports.main status"
