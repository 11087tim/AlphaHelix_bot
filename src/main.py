from __future__ import annotations

import logging
import sys
from datetime import datetime

# 支援兩種執行方式：`python -m src.main`（套件）或 `python src/main.py`（腳本）
if __package__:
    from .config import Config, ConfigError, load_config
    from . import (x_client, summarizer, site_generator, emailer, publisher,
                   graph_link, reports_bridge, memory_link)
    from .storage import Storage
    from .digest_store import DigestStore
    from .pending_store import PendingStore, SNAPSHOT_PATH
else:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.config import Config, ConfigError, load_config
    from src import (x_client, summarizer, site_generator, emailer, publisher,
                     graph_link, reports_bridge, memory_link)
    from src.storage import Storage
    from src.digest_store import DigestStore
    from src.pending_store import PendingStore, SNAPSHOT_PATH

# 網站首頁最多顯示最近幾份彙整的可折疊區塊
SITE_HOURS = 60

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


def _dedup(tweets: list[dict]) -> list[dict]:
    """依 id 去重，保留首次出現（帳號來源優先於關鍵字，因 _collect 先加帳號）。"""
    seen, out = set(), []
    for t in tweets:
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        out.append(t)
    return out


def _resolve_describe_media(cfg: Config) -> bool:
    """是否用視覺模型描述圖片：需開啟 describe、且模型支援圖片輸入。"""
    if not (cfg.media_enabled and cfg.media_describe):
        return False
    if not summarizer.model_supports_vision(cfg.openrouter_model, cfg.openrouter_api_key):
        logger.warning("模型 %s 不支援圖片輸入，本次僅以文字分析（不描述圖片）。", cfg.openrouter_model)
        return False
    return True


def _analyze(label: str, tweets: list[dict], cfg: Config, describe_media: bool,
             graph_context: str | None = None) -> dict | None:
    result = summarizer.summarize_group(
        tweets, label, cfg.openrouter_api_key, cfg.openrouter_model,
        describe_media=describe_media,
        system_prompt=cfg.openrouter_system_prompt or None,
        graph_context=graph_context,
    )
    if result and result["summary"]:
        return {"label": label, "summary": result["summary"], "references": result["references"]}
    return None


def run_fetch(cfg: Config) -> int:
    """每小時執行：抓取各追蹤來源的新貼文，累積到待彙整區（不做 LLM、不更新網站、不寄信）。"""
    client = x_client._build_client(cfg.x_bearer_token)
    storage = Storage()
    pending = PendingStore()

    _, _, all_tweets = _collect(cfg, client, storage)

    if not cfg.media_enabled:
        for t in all_tweets:
            t["media"] = []

    new_tweets = storage.filter_new(_dedup(all_tweets))
    if new_tweets:
        pending.add(new_tweets)
        storage.mark_seen(new_tweets)

    storage.save()   # 持久化帳號 ID 快取與 seen id
    pending.save()
    logger.info("本時段抓取完成：新增 %d 則待彙整（累積 %d 則）。", len(new_tweets), len(pending.all()))
    return 0


def _extra_context(graph_context: str | None, tweets: list[dict],
                   timeline: str | None = None) -> str | None:
    """把產業關係圖、財報事實卡、先前觀點時間線併成一段參考文字附在 prompt 末尾。"""
    cards = reports_bridge.load_report_cards(tweets)  # 台股 X×財報跨源印證
    return "\n\n".join(x for x in (graph_context, cards, timeline) if x) or None


def _synthesize(cfg: Config, tweets: list[dict]) -> dict | None:
    """對一批推文做跨作者觀點彙整，回傳 digest entry（無實質內容則回 None）。不含存檔/推送/寄信。"""
    describe_media = _resolve_describe_media(cfg)
    graph_context = graph_link.load_graph_context()  # 讓 Opus 判讀時可延伸供應鏈關聯
    # 跨時間記憶：讀最近幾份 digest 組成觀點時間線，讓 Opus 於頂部產出「📈 本期變化」
    timeline = memory_link.build_timeline(DigestStore().recent(memory_link.MAX_DIGESTS))

    # 所有帳號作者合成一份跨作者觀點分析；關鍵字各自一份（本身即跨作者）
    account_tweets = [t for t in tweets if str(t.get("source", "")).startswith("account:")]
    keyword_map: dict[str, list[dict]] = {}
    for t in tweets:
        src = str(t.get("source", ""))
        if src.startswith("keyword:"):
            keyword_map.setdefault(src.split("keyword:", 1)[1], []).append(t)

    account_sections = []
    if account_tweets:
        # 時間線只給帳號這組（主彙整），「📈 本期變化」放這組頂部即為整份 digest 之首
        sec = _analyze("追蹤作者", account_tweets, cfg, describe_media,
                       _extra_context(graph_context, account_tweets, timeline))
        if sec:
            sec["label"] = ""  # 跨作者彙整，不用單一 handle 當標題
            account_sections = [sec]

    keyword_sections = []
    for kw, kws in keyword_map.items():
        sec = _analyze(kw, kws, cfg, describe_media, _extra_context(graph_context, kws))
        if sec:
            keyword_sections.append(sec)

    if not account_sections and not keyword_sections:
        return None

    now = datetime.now()
    return {
        "id": now.strftime("%Y%m%d-%H%M"),
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "model": cfg.openrouter_model,
        "account_sections": account_sections,
        "keyword_sections": keyword_sections,
    }


def run_synthesis(cfg: Config) -> int:
    """每天兩次執行：對累積的所有作者貼文做跨作者觀點彙整 → 更新網站 → 自動 push → 寄信 → 清空待彙整。"""
    pending = PendingStore()
    tweets = pending.all()
    if not tweets:
        logger.info("沒有待彙整的貼文，跳過。")
        return 0

    # 清空 pending 前先存快照，讓改 prompt 後可用 resynth 免費重跑同一批（不必再花錢 fetch）
    snapshot = PendingStore(SNAPSHOT_PATH)
    snapshot.clear()
    snapshot.add(list(tweets))
    snapshot.save()

    entry = _synthesize(cfg, tweets)
    if entry is None:
        pending.clear()
        pending.save()
        logger.info("彙整後無實質內容（可能全為宣傳），清空待彙整。")
        return 0

    digest_store = DigestStore()
    digest_store.append(entry)
    site_generator.render_site(cfg.site_title, digest_store.recent(SITE_HOURS), cfg.site_output_dir)
    digest_store.save()

    if cfg.site_auto_push:
        publisher.publish_docs()

    if cfg.email_to:
        html = site_generator.render_email(cfg.site_title, [entry], cfg.site_url)
        subject = f"{cfg.email_subject_prefix} {entry['generated_at']} 觀點彙整"
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

    # 彙整成功並存檔後才清空待彙整（避免中途失敗導致漏掉）
    pending.clear()
    pending.save()
    logger.info("完成彙整：帳號觀點 %d 段、關鍵字 %d 段。",
                len(entry["account_sections"]), len(entry["keyword_sections"]))
    return 0


def run_resynth(cfg: Config) -> int:
    """用快照（或目前 pending）重跑彙整，只在本機產生預覽網站；不推送、不寄信、不寫入 digest、不清空。
    用途：改 prompt / graph 後，免費在同一批推文上反覆看效果。"""
    snapshot = PendingStore(SNAPSHOT_PATH)
    tweets = snapshot.all()
    source = "snapshot.json"
    if not tweets:  # 還沒有快照就退回目前累積中的 pending（同樣不清空）
        tweets = PendingStore().all()
        source = "pending.json"
    if not tweets:
        logger.info("沒有可重跑的推文（snapshot.json 與 pending.json 都是空的）。")
        return 0

    logger.info("resynth：使用 %s 的 %d 則推文重跑（不推送/不寄信/不清空）。", source, len(tweets))
    entry = _synthesize(cfg, tweets)
    if entry is None:
        logger.info("重跑後無實質內容。")
        return 0

    # 預覽：把這次重跑的結果疊在既有歷史時段之上，但不寫回 digest_store
    recent = DigestStore().recent(SITE_HOURS)
    preview = [entry] + [d for d in recent if d.get("id") != entry["id"]]
    site_generator.render_site(cfg.site_title, preview, cfg.site_output_dir)
    logger.info("預覽已更新：開 %s/index.html 檢視（未推送、未寄信、未寫入 digest）。", cfg.site_output_dir)
    return 0


def run_render(cfg: Config) -> int:
    """只用既有 digests 重新產生網站（不抓取、不呼叫 LLM）；改版型/樣板後用來更新。"""
    digest_store = DigestStore()
    digests = digest_store.recent(SITE_HOURS)
    if not digests:
        logger.info("沒有既有 digest 可重新渲染。")
        return 0
    site_generator.render_site(cfg.site_title, digests, cfg.site_output_dir)
    if cfg.site_auto_push:
        publisher.publish_docs()
    logger.info("已重新渲染網站（%d 個時段）。", len(digests))
    return 0


def run(mode: str) -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        logger.error("設定錯誤：%s", exc)
        return 1

    if mode in ("synthesis", "email"):  # email 為舊名相容
        return run_synthesis(cfg)
    if mode == "fetch":
        return run_fetch(cfg)
    if mode == "render":  # 只重新產生網站（樣板改版後用）
        return run_render(cfg)
    if mode == "resynth":  # 用快照重跑彙整、只出本機預覽（改 prompt 後免費看效果）
        return run_resynth(cfg)
    if mode == "run":  # 一次跑完：收集 → 彙整（適合每天固定幾次觸發）
        run_fetch(cfg)
        return run_synthesis(cfg)
    logger.error("未知模式：%s（可用：fetch / synthesis / render / resynth / run）", mode)
    return 2


if __name__ == "__main__":
    # 用法：python -m src.main [fetch|synthesis|render|resynth|run]，預設 fetch
    mode = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    sys.exit(run(mode))
