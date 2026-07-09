from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import ReportsConfig
from .mops_client import MopsClient, MopsError
from .storage import ReportStorage

logger = logging.getLogger(__name__)


def _fetch_company_year(client: MopsClient, storage: ReportStorage,
                        co_id: str, year: int, quarters: list[int],
                        report_types: list[str], language: str) -> dict:
    """處理單一「公司-年」：列一次清單，再逐季下載。回傳統計。"""
    stats = {"done": 0, "missing": 0, "error": 0}
    try:
        files = client.list_year(co_id, year)
    except MopsError as exc:
        for q in quarters:
            storage.mark_failed(co_id, year, q, language, f"list 失敗：{exc}")
        stats["error"] += len(quarters)
        logger.warning("[%s %d] 列表失敗：%s", co_id, year, exc)
        return stats

    for q in quarters:
        filename, rt = client.pick_filename(files, co_id, year, q, report_types, language)
        if not filename:
            storage.mark_failed(co_id, year, q, language, "查無符合類型/語言的財報")
            stats["missing"] += 1
            continue
        try:
            content = client.download(co_id, filename)
            storage.save_pdf(co_id, year, q, rt, language, filename, content)
            stats["done"] += 1
            logger.info("[%s %dQ%d] ✓ %s (%s/%s, %.1fMB)", co_id, year, q, filename, rt, language, len(content) / 1e6)
        except MopsError as exc:
            storage.mark_failed(co_id, year, q, language, f"下載失敗：{exc}")
            stats["error"] += 1
            logger.warning("[%s %dQ%d] 下載失敗：%s", co_id, year, q, exc)
    return stats


def run_fetch(cfg: ReportsConfig) -> int:
    client = MopsClient(cfg.min_interval_sec, cfg.max_retries)
    storage = ReportStorage(cfg.data_dir)

    # 建立「公司-年」任務，每組帶尚未完成的季別（可續跑）
    groups = []
    skipped = 0
    for co_id in cfg.stocks:
        for year in cfg.years:
            todo = [q for q in cfg.quarters if not storage.is_done(co_id, year, q, cfg.language)]
            skipped += len(cfg.quarters) - len(todo)
            if todo:
                groups.append((co_id, year, todo))

    total_tasks = sum(len(g[2]) for g in groups)
    logger.info("待抓 %d 個公司-年（共 %d 份季報），已完成略過 %d 份，workers=%d",
                len(groups), total_tasks, skipped, cfg.workers)
    if not groups:
        logger.info("沒有待抓的財報，全部已完成。")
        return 0

    agg = {"done": 0, "missing": 0, "error": 0}
    with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
        futures = {
            ex.submit(_fetch_company_year, client, storage, co_id, year, qs,
                      cfg.report_types, cfg.language):
                (co_id, year)
            for (co_id, year, qs) in groups
        }
        completed = 0
        for fut in as_completed(futures):
            co_id, year = futures[fut]
            try:
                s = fut.result()
                for k in agg:
                    agg[k] += s[k]
            except Exception as exc:  # noqa: BLE001
                logger.error("[%s %d] 未預期錯誤：%s", co_id, year, exc)
            completed += 1
            if completed % 10 == 0 or completed == len(futures):
                storage.save()  # 定期存檔點，斷了可續跑
                logger.info("進度：%d/%d 個公司-年完成", completed, len(futures))

    storage.save()
    logger.info("完成。成功 %d 份、查無 %d 份、失敗 %d 份。",
                agg["done"], agg["missing"], agg["error"])
    logger.info("PDF 存於 %s，狀態見 %s", storage.raw_dir, storage.manifest_path)
    return 0
