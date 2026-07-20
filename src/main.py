from __future__ import annotations

import logging
import os
import socket
import sys
import time
from datetime import datetime

# 支援兩種執行方式：`python -m src.main`（套件）或 `python src/main.py`（腳本）
if __package__:
    from .config import Config, ConfigError, load_config
    from . import (x_client, summarizer, site_generator, emailer, publisher,
                   graph_link, reports_bridge, memory_link, memory_extract,
                   podcast_client, transcribe, podcast_distill, youtube_client)
    from .storage import Storage
    from .digest_store import DigestStore
    from .pending_store import PendingStore, SNAPSHOT_PATH
    from .memory_store import MemoryStore
else:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.config import Config, ConfigError, load_config
    from src import (x_client, summarizer, site_generator, emailer, publisher,
                     graph_link, reports_bridge, memory_link, memory_extract,
                     podcast_client, transcribe, podcast_distill, youtube_client)
    from src.storage import Storage
    from src.digest_store import DigestStore
    from src.pending_store import PendingStore, SNAPSHOT_PATH
    from src.memory_store import MemoryStore

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


def _wait_for_network(host: str = "api.twitter.com", timeout: int = 120, delay: int = 15) -> bool:
    """等網路就緒（排程在 Mac 剛喚醒時觸發、Wi-Fi 可能還沒連上）。能解析 host 即視為就緒。
    以「牆上時間」設總上限（會隨睡眠前進），避免 Mac 反覆入睡把重試拖成數小時。"""
    deadline = time.time() + timeout
    while True:
        try:
            socket.getaddrinfo(host, 443)
            return True
        except socket.gaierror:
            if time.time() + delay >= deadline:
                return False
            logger.warning("網路尚未就緒（無法解析 %s），%d 秒後重試…", host, delay)
            time.sleep(delay)


def run_fetch(cfg: Config) -> int:
    """每小時執行：抓取各追蹤來源的新貼文，累積到待彙整區（不做 LLM、不更新網站、不寄信）。"""
    if not _wait_for_network():
        logger.error("網路持續無法連線，略過本次抓取（避免整批失敗；下次排程再試）。")
        return 0
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

    # 所有帳號作者合成一份跨作者觀點分析；關鍵字各自一份（本身即跨作者）
    account_tweets = [t for t in tweets if str(t.get("source", "")).startswith("account:")]
    keyword_map: dict[str, list[dict]] = {}
    for t in tweets:
        src = str(t.get("source", ""))
        if src.startswith("keyword:"):
            keyword_map.setdefault(src.split("keyword:", 1)[1], []).append(t)

    # 跨時間記憶：依本批提到的實體撈歷史立場軌跡，讓 Opus 於頂部產出「📈 本期變化」
    timeline = memory_link.build_timeline(account_tweets, DigestStore().recent(memory_link.MAX_DIGESTS))

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

    # 長訪談/論壇（Podcast + YouTube 蒸餾要點）自成頂層分類，享有同樣的 graph/財報 延伸
    podcast_items = [t for t in tweets
                     if str(t.get("source", "")).startswith(("podcast:", "youtube:"))]
    podcast_sections = []
    if podcast_items:
        sec = _analyze("", podcast_items, cfg, describe_media,
                       _extra_context(graph_context, podcast_items))
        if sec:
            podcast_sections = [sec]

    if not account_sections and not keyword_sections and not podcast_sections:
        return None

    now = datetime.now()
    return {
        "id": now.strftime("%Y%m%d-%H%M"),
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "model": cfg.openrouter_model,
        "account_sections": account_sections,
        "keyword_sections": keyword_sections,
        "podcast_sections": podcast_sections,
    }


def _update_memory(cfg: Config, entry: dict) -> None:
    """把剛產出的 digest 萃取成立場紀錄寫入記憶帳本。任何失敗都不得影響主流程。"""
    try:
        recs = memory_extract.extract_records(entry, cfg.memory_model, cfg.openrouter_api_key)
        if recs:
            store = MemoryStore()
            store.add_records(recs)
            store.save()
    except Exception as exc:  # noqa: BLE001
        logger.warning("更新記憶帳本失敗（不影響主流程）：%s", exc)


def run_memory_backfill(cfg: Config) -> int:
    """從既有 digests.json 回填記憶帳本（跳過已萃取過的 digest）。"""
    store = MemoryStore()
    digests = DigestStore().digests  # 由舊到新
    added = 0
    for entry in digests:
        if store.has_digest(entry.get("id", "")):
            continue
        recs = memory_extract.extract_records(entry, cfg.memory_model, cfg.openrouter_api_key)
        if recs:
            store.add_records(recs)
            added += len(recs)
    store.save()
    logger.info("記憶回填完成：新增 %d 筆立場紀錄（掃描 %d 份 digest）。", added, len(digests))
    return 0


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

    _update_memory(cfg, entry)  # 萃取立場寫入記憶帳本（供下次「本期變化」；失敗不影響主流程）

    if cfg.site_auto_push:
        publisher.publish_docs()

    # 收件人：dev 每次都寄；prod 只在指定整點（預設 8、20）寄，避免正式信箱被測試信洗版。
    # 手動補跑「正式版」可設 XBOT_FORCE_PROD=1 強制納入 prod（不限時段）。
    recipients = list(cfg.email_dev)
    if os.environ.get("XBOT_FORCE_PROD") == "1" or datetime.now().hour in cfg.email_prod_hours:
        recipients += [a for a in cfg.email_prod if a not in recipients]
    if recipients:
        html = site_generator.render_email(cfg.site_title, [entry], cfg.site_url,
                                           window_hours=cfg.fetch_window_hours)
        subject = f"{cfg.email_subject_prefix} {entry['generated_at']} 觀點彙整"
        try:
            emailer.send_html_email(
                gmail_address=cfg.gmail_address,
                gmail_app_password=cfg.gmail_app_password,
                to=recipients,
                subject=subject,
                html_body=html,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("寄信失敗：%s", exc)

    # 彙整成功並存檔後才清空待彙整（避免中途失敗導致漏掉）
    pending.clear()
    pending.save()
    logger.info("完成彙整：帳號觀點 %d 段、關鍵字 %d 段、Podcast/YouTube %d 段。",
                len(entry["account_sections"]), len(entry["keyword_sections"]),
                len(entry.get("podcast_sections", [])))
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


def run_podcast(cfg: Config) -> int:
    """抓長訪談新集 → Whisper 轉錄 → 蒸餾成投資要點 → 加入 pending（下次 synthesis 會納入）。"""
    if not cfg.podcast_enabled:
        logger.info("podcasts.enabled 未開或無 feeds，略過。")
        return 0
    if not cfg.groq_api_key:
        logger.error("缺少 GROQ_API_KEY（長訪談轉錄需要）。請到 console.groq.com 取得後填入 .env。")
        return 1

    seen = podcast_client.SeenStore()
    episodes = podcast_client.fetch_new_episodes(
        cfg.podcast_feeds, cfg.podcast_window_hours, cfg.podcast_max_episodes, seen)
    if not episodes:
        logger.info("沒有需要處理的新集。")
        return 0

    pending = PendingStore()
    total = 0
    for ep in episodes:
        logger.info("處理：%s — %s", ep.get("show"), ep.get("title"))
        try:
            transcript = transcribe.transcribe_url(
                ep["audio_url"], cfg.groq_api_key, cfg.whisper_model)
            items = podcast_distill.distill(ep, transcript, cfg.memory_model, cfg.openrouter_api_key)
        except Exception as exc:  # noqa: BLE001
            logger.error("處理失敗（保留未讀，下次重試）：%s", exc)
            continue
        pending.add(items)
        seen.mark(ep["id"])  # 成功才標記，失敗留待下次
        total += len(items)

    pending.save()
    seen.save()
    logger.info("長訪談處理完成：新增 %d 條要點到 pending（來自 %d 集）。", total, len(episodes))
    return 0


def run_youtube(cfg: Config) -> int:
    """抓 YouTube 頻道新片 → 免費字幕 → 蒸餾成投資要點 → 加入 pending（下次 synthesis 納入）。"""
    if not cfg.youtube_enabled:
        logger.info("youtube.enabled 未開或無 channels，略過。")
        return 0
    seen = podcast_client.SeenStore()  # 與 podcast 共用「已處理媒體」記錄
    videos = youtube_client.fetch_new_videos(
        cfg.youtube_channels, cfg.youtube_window_hours, cfg.youtube_max_videos, seen)
    if not videos:
        logger.info("沒有需要處理的新影片。")
        return 0

    pending = PendingStore()
    total = 0
    for v in videos:
        logger.info("處理 YouTube：%s — %s", v.get("show"), v.get("title"))
        try:
            transcript = youtube_client.get_transcript(v["video_id"])
            items = podcast_distill.distill(v, transcript, cfg.memory_model, cfg.openrouter_api_key)
        except Exception as exc:  # noqa: BLE001
            logger.error("處理失敗（保留未讀，下次重試）：%s", exc)
            continue
        for it in items:  # 標成 youtube 來源，仍歸「🎙️ Podcast／YouTube」段
            it["source"] = f"youtube:{v.get('show', 'YouTube')}"
        pending.add(items)
        seen.mark(v["id"])
        total += len(items)

    pending.save()
    seen.save()
    logger.info("YouTube 處理完成：新增 %d 條要點（來自 %d 支影片）。", total, len(videos))
    return 0


def run_youtube_seed(cfg: Config) -> int:
    """把各頻道目前影片設為基準（已讀），之後只處理新上片。"""
    if not cfg.youtube_channels:
        logger.info("沒有設定 youtube channels。")
        return 0
    seen = podcast_client.SeenStore()
    n = youtube_client.mark_all_seen(cfg.youtube_channels, seen)
    seen.save()
    logger.info("已標記 %d 支影片為基準；往後只處理新上片。", n)
    return 0


def run_longform(cfg: Config) -> int:
    """長內容一次跑完：Podcast + YouTube（供每日排程用）。"""
    rc = run_podcast(cfg)
    ry = run_youtube(cfg)
    return rc or ry


def run_leverage(cfg: Config) -> int:
    """每交易日晚間執行：增量抓台股融資融券/不限用途 → 重建槓桿儀表板 → push docs。
    回抓近 7 天做視窗覆蓋（補假日/延遲更新的漏；不丟舊歷史）。資料只在 VM 上長。"""
    from datetime import date, timedelta

    from . import leverage_ingest, leverage_dashboard

    end = date.today()
    start = end - timedelta(days=7)
    try:
        leverage_ingest.ingest(start.isoformat(), end.isoformat())
    except Exception as exc:  # noqa: BLE001
        logger.error("槓桿資料抓取失敗（保留既有歷史，不更新儀表板）：%s", exc)
        return 1
    out = leverage_dashboard.build()
    logger.info("已重建槓桿儀表板：%s", out)
    if cfg.site_auto_push:
        publisher.publish_docs()
    return 0


def run_podcast_seed(cfg: Config) -> int:
    """把所有 feed 目前的集數標為基準（已讀），之後 podcast 只處理新發布的集（避免回填整批舊集）。"""
    if not cfg.podcast_feeds:
        logger.info("沒有設定 podcast feeds。")
        return 0
    seen = podcast_client.SeenStore()
    n = podcast_client.mark_all_seen(cfg.podcast_feeds, seen)
    seen.save()
    logger.info("已標記 %d 集為基準；往後只處理新發布的集。", n)
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
    if mode == "memory-backfill":  # 從既有 digests.json 回填記憶帳本
        return run_memory_backfill(cfg)
    if mode == "podcast":  # 抓長訪談新集 → 轉錄 → 蒸餾 → 加入 pending
        return run_podcast(cfg)
    if mode == "podcast-seed":  # 把目前集數設為基準，之後只處理新集
        return run_podcast_seed(cfg)
    if mode == "youtube":  # 抓 YouTube 頻道新片 → 字幕 → 蒸餾 → 加入 pending
        return run_youtube(cfg)
    if mode == "youtube-seed":  # 把目前影片設為基準，之後只處理新上片
        return run_youtube_seed(cfg)
    if mode == "longform":  # Podcast + YouTube 一次跑完（每日排程用）
        return run_longform(cfg)
    if mode == "leverage":  # 增量抓台股槓桿資料 → 重建儀表板 → push（每交易日晚間）
        return run_leverage(cfg)
    if mode == "run":  # 一次跑完：收集 → 彙整（適合每天固定幾次觸發）
        run_fetch(cfg)
        return run_synthesis(cfg)
    logger.error("未知模式：%s（可用：fetch / synthesis / render / resynth / memory-backfill / "
                 "podcast / podcast-seed / youtube / youtube-seed / longform / leverage / run）", mode)
    return 2


if __name__ == "__main__":
    # 用法：python -m src.main [fetch|synthesis|render|resynth|memory-backfill|podcast|run]，預設 fetch
    mode = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    sys.exit(run(mode))
