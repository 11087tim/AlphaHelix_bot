from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
    )


def publish_docs(docs_rel: str = "docs", repo_dir: Path = PROJECT_ROOT) -> bool:
    """把 docs/ 的變動自動 commit & push 到 origin。
    無變動則跳過；任何步驟失敗都只記錄、不拋出，避免影響抓取/摘要流程。
    回傳是否有成功 push。"""
    try:
        add = _git(["add", docs_rel], repo_dir)
        if add.returncode != 0:
            logger.error("自動 push：git add 失敗：%s", add.stderr.strip())
            return False

        # 檢查 docs/ 是否真的有變動被 stage（rc==0 代表無差異）
        diff = _git(["diff", "--cached", "--quiet", "--", docs_rel], repo_dir)
        if diff.returncode == 0:
            logger.info("自動 push：docs/ 無變動，略過。")
            return False

        msg = f"chore: 自動更新摘要網站 {datetime.now().strftime('%Y-%m-%d %H:%M')} [auto]"
        commit = _git(["commit", "-m", msg, "--", docs_rel], repo_dir)
        if commit.returncode != 0:
            logger.error(
                "自動 push：git commit 失敗：%s",
                commit.stderr.strip() or commit.stdout.strip(),
            )
            return False

        push = _git(["push", "origin", "HEAD"], repo_dir)
        if push.returncode != 0:
            logger.error("自動 push：git push 失敗（下次會再嘗試）：%s", push.stderr.strip())
            return False

        logger.info("自動 push：已把網站更新推送到 GitHub。")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("自動 push 發生例外（略過）：%s", exc)
        return False
