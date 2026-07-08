from __future__ import annotations

import logging
import sys
from datetime import datetime

# 支援兩種執行方式：`python -m src.main`（套件）或 `python src/main.py`（腳本）
if __package__:
    from .config import Config, ConfigError, load_config
    from . import x_client, summarizer, site_generator, emailer
    from .storage import Storage
else:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.config import Config, ConfigError, load_config
    from src import x_client, summarizer, site_generator, emailer
    from src.storage import Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("xbot")


def _collect(cfg: Config, client) -> tuple[list[dict], list[dict], list[dict]]:
    """回傳 (account_groups, keyword_groups, all_tweets)。group = {label, tweets}。"""
    account_groups: list[dict] = []
    keyword_groups: list[dict] = []
    all_tweets: list[dict] = []

    for username in cfg.accounts:
        try:
            tweets = x_client.get_user_tweets(client, username, cfg.max_results_per_source)
            account_groups.append({"label": username, "tweets": tweets})
            all_tweets.extend(tweets)
        except Exception as exc:  # noqa: BLE001
            logger.warning("抓取帳號 @%s 失敗：%s", username, exc)

    for keyword in cfg.keywords:
        try:
            tweets = x_client.search_recent(client, keyword, cfg.max_results_per_source)
            keyword_groups.append({"label": keyword, "tweets": tweets})
            all_tweets.extend(tweets)
        except Exception as exc:  # noqa: BLE001
            logger.warning("搜尋關鍵字「%s」失敗：%s", keyword, exc)

    return account_groups, keyword_groups, all_tweets


def _summarize_groups(groups: list[dict], storage: Storage, cfg: Config) -> list[dict]:
    sections = []
    for group in groups:
        new_tweets = storage.filter_new(group["tweets"])
        if not new_tweets:
            continue
        summary = summarizer.summarize_group(
            new_tweets, group["label"], cfg.openrouter_api_key, cfg.openrouter_model
        )
        if summary:
            sections.append({"label": group["label"], "summary": summary})
    return sections


def run() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        logger.error("設定錯誤：%s", exc)
        return 1

    client = x_client._build_client(cfg.x_bearer_token)
    storage = Storage()

    account_groups, keyword_groups, all_tweets = _collect(cfg, client)

    account_sections = _summarize_groups(account_groups, storage, cfg)
    keyword_sections = _summarize_groups(keyword_groups, storage, cfg)

    if not account_sections and not keyword_sections:
        logger.info("沒有新的推文可摘要，結束（不產生網站也不寄信）。")
        return 0

    html = site_generator.render_digest(
        cfg.site_title, account_sections, keyword_sections, cfg.site_output_dir
    )

    if cfg.email_to:
        subject = f"{cfg.email_subject_prefix} {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        try:
            emailer.send_html_email(
                gmail_address=cfg.gmail_address,
                gmail_app_password=cfg.gmail_app_password,
                to=cfg.email_to,
                subject=subject,
                html_body=html,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("寄信失敗：%s", exc)

    # 全部成功處理後才記錄 seen id，避免中途失敗導致漏摘要
    storage.mark_seen(all_tweets)
    storage.save()
    logger.info("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(run())
