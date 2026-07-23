"""一鍵更新 nash-ai token（在 Mac 上跑）。

開一個瀏覽器視窗到 nash-ai 登入頁 → 你用手機微信掃碼 →
腳本自動偵測 localStorage 拿到 token → 自動 scp 推上 VM → 驗證。

用法：
    cd ~/Alphehelix_X_bot && .venv/bin/python -m hot_reports.refresh_token
"""
from __future__ import annotations

import subprocess
import sys
import time

VM_HOST = "alphahelix_vm"
VM_TOKEN_PATH = "~/Alphehelix_X_bot/hot_reports_data/token.txt"
LOGIN_URL = "https://www.nash-ai.cn/login.html"
WAIT_SEC = 180


def grab_token() -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        print(f"請用手機微信掃描視窗中的二維碼（{WAIT_SEC} 秒內）…")
        deadline = time.time() + WAIT_SEC
        token = ""
        while time.time() < deadline:
            token = page.evaluate("localStorage.getItem('token') || ''")
            if token:
                break
            time.sleep(2)
        browser.close()
    if not token:
        raise SystemExit("等不到 token，請重跑再掃一次。")
    print(f"已取得 token（{len(token)} 字元）")
    return token


def push_to_vm(token: str) -> None:
    subprocess.run(["ssh", VM_HOST, f"mkdir -p $(dirname {VM_TOKEN_PATH})"], check=True)
    subprocess.run(["ssh", VM_HOST, f"cat > {VM_TOKEN_PATH}"],
                   input=token.encode(), check=True)
    print(f"已推上 {VM_HOST}:{VM_TOKEN_PATH}，驗證中…")
    subprocess.run(
        ["ssh", VM_HOST,
         "cd ~/Alphehelix_X_bot && .venv/bin/python -m hot_reports.main status"],
        check=True)


def main() -> int:
    token = grab_token()
    # 本機也留一份（本機手動跑 pipeline 時用）
    from . import config
    config.ensure_dirs()
    config.TOKEN_PATH.write_text(token)
    push_to_vm(token)
    print("完成！今晚 23:00 排程會用新 token。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
