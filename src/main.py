from __future__ import annotations

import logging
import sys
from datetime import datetime

# 支援兩種執行方式：`python -m src.main`（套件）或 `python src/main.py`（腳本）
if __package__:
    from .config import Config, ConfigError, load_config
    from . import x_client, summarizer, site_generator, emailer, publisher
    from .storage import Storage
    from .digest_store import DigestStore
else:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.config import Config, ConfigError, load_config
    from src import x_client, summarizer, site_generator, emailer, publisher
    from src.storage import Storage
    from src.digest_store import DigestStore

# 網站首頁最多顯示最近幾個時段的可折疊區塊
SITE_HOURS = 48

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("xbot")


def _collect(cfg: Config, client, storage: Storage) -> tuple[list[dict], list[dict], list[dict]]:
    """回傳 (account_groups, keyword_groups, all_tweets)。group = {label, tweets}。"""
    account_groups: list[dict] = []
    keyword_groups: list[dict] = []
    all_tweets: list[dict] = []

    for username in cfg.accounts:
        try:
            # 帳號 ID 固定，查一次就快取起來，之後省下每小時的 User: Read
            user_id = storage.get_user_id(username)
            if user_id is None:
                user_id = x_client.get_user_id(client, username)
                if user_id is None:
                    logger.warning("找不到帳號 @%s，略過。", username)
                    continue
                storage.set_user_id(username, user_id)

            tweets = x_client.get_user_tweets(
                client, username, cfg.max_results_per_source, cfg.fetch_window_hours, user_id
            )
            account_groups.append({"label": username, "tweets": tweets})
            all_tweets.extend(tweets)
        except Exception as exc:  # noqa: BLE001
            logger.warning("抓取帳號 @%s 失敗：%s", username, exc)

    for keyword in cfg.keywords:
        try:
            tweets = x_client.search_recent(
                client, keyword, cfg.max_results_per_source, cfg.fetch_window_hours
            )
            keyword_groups.append({"label": keyword, "tweets": tweets})
            all_tweets.extend(tweets)
        except Exception as exc:  # noqa: BLE001
            logger.warning("搜尋關鍵字「%s」失敗：%s", keyword, exc)

    return account_groups, keyword_groups, all_tweets


def _summarize_groups(
    groups: list[dict], storage: Storage, cfg: Config, describe_media: bool
) -> list[dict]:
    sections = []
    for group in groups:
        new_tweets = storage.filter_new(group["tweets"])
        if not new_tweets:
            continue
        result = summarizer.summarize_group(
            new_tweets, group["label"], cfg.openrouter_api_key, cfg.openrouter_model,
            describe_media=describe_media,
            system_prompt=cfg.openrouter_system_prompt or None,
        )
        if result and result["summary"]:
            sections.append(
                {
                    "label": group["label"],
                    "summary": result["summary"],
                    "references": result["references"],
                }
            )
    return sections


def run_fetch(cfg: Config) -> int:
    """每小時執行：抓取 → 摘要 → 存成一個時段的 digest → 更新網站（不寄信）。"""
    client = x_client._build_client(cfg.x_bearer_token)
    storage = Storage()
    digest_store = DigestStore()

    account_groups, keyword_groups, all_tweets = _collect(cfg, client, storage)

    # 若不啟用媒體，直接清掉抓到的媒體，後續就不會描述也不會顯示
    if not cfg.media_enabled:
        for t in all_tweets:
            t["media"] = []

    # 是否用視覺模型描述圖片：需開啟 describe、且模型支援圖片輸入
    describe_media = cfg.media_enabled and cfg.media_describe
    if describe_media and not summarizer.model_supports_vision(cfg.openrouter_model, cfg.openrouter_api_key):
        logger.warning("模型 %s 不支援圖片輸入，本次僅以文字摘要（不描述圖片）。", cfg.openrouter_model)
        describe_media = False

    account_sections = _summarize_groups(account_groups, storage, cfg, describe_media)
    keyword_sections = _summarize_groups(keyword_groups, storage, cfg, describe_media)

    if not account_sections and not keyword_sections:
        # 即使沒有新推文，也把這次查到的帳號 ID 快取存下來，避免下一小時又重查
        storage.save()
        logger.info("這一小時沒有新推文，跳過（不建立時段、不更新網站）。")
        return 0

    now = datetime.now()
    entry = {
        "id": now.strftime("%Y%m%d-%H%M"),
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "model": cfg.openrouter_model,
        "account_sections": account_sections,
        "keyword_sections": keyword_sections,
        "emailed": False,
    }
    digest_store.append(entry)

    site_generator.render_site(cfg.site_title, digest_store.recent(SITE_HOURS), cfg.site_output_dir)

    # 全部成功後才記錄 seen id 與 digest，避免中途失敗導致漏摘要
    storage.mark_seen(all_tweets)
    storage.save()
    digest_store.save()

    # 自動把網站更新推送到 GitHub（GitHub Pages 會自動重新部署）
    if cfg.site_auto_push:
        publisher.publish_docs()

    logger.info("完成本時段抓取。")
    return 0


def run_email(cfg: Config) -> int:
    """每天三次執行：把尚未寄出的時段摘要合併成一封信寄出。"""
    if not cfg.email_to:
        logger.info("未設定收件人，跳過寄信。")
        return 0

    digest_store = DigestStore()
    unsent = digest_store.unsent()
    if not unsent:
        logger.info("沒有待寄的時段摘要，跳過寄信。")
        return 0

    # 由新到舊呈現
    html = site_generator.render_email(cfg.site_title, list(reversed(unsent)), cfg.site_url)
    subject = f"{cfg.email_subject_prefix} {datetime.now().strftime('%Y-%m-%d %H:%M')}（{len(unsent)} 個時段）"
    try:
        emailer.send_html_email(
            gmail_address=cfg.gmail_address,
            gmail_app_password=cfg.gmail_app_password,
            to=cfg.email_to,
            subject=subject,
            html_body=html,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("寄信失敗（不標記為已寄，下次會重試）：%s", exc)
        return 1

    digest_store.mark_emailed([d["id"] for d in unsent])
    digest_store.save()
    logger.info("完成寄信，共 %d 個時段。", len(unsent))
    return 0


def run(mode: str) -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        logger.error("設定錯誤：%s", exc)
        return 1

    if mode == "email":
        return run_email(cfg)
    if mode == "fetch":
        return run_fetch(cfg)
    logger.error("未知模式：%s（可用：fetch / email）", mode)
    return 2


if __name__ == "__main__":
    # 用法：python -m src.main [fetch|email]，預設 fetch
    mode = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    sys.exit(run(mode))
