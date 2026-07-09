from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import ReportsConfig
from . import llm
from .analyze import analyze_report
from .storage import ReportStorage

logger = logging.getLogger(__name__)

AGG_SYSTEM = (
    "你是資深投資分析師。以下是某台股公司連續數季的財報重點彙整（每季一段）。"
    "請跨季『彙集』做趨勢分析，要有判斷、附具體數字對比：\n"
    "1. 營收、毛利/獲利、EPS 的逐季趨勢與轉折點。\n"
    "2. 分部/產品線/地區/客戶結構隨時間的變化。\n"
    "3. 資產負債與財務體質變化（存貨、應收、資本支出、負債、現金）。\n"
    "4. 風險項目（客戶或地緣集中度、或有負債、關係人往來、質押）隨時間的演變。\n"
    "5. 綜合評估這家公司近幾季的走向與投資意涵，最後一行用「一句話：」總結。\n"
    "只根據提供內容判讀，不要杜撰數字。繁體中文，用小標＋條列，精簡有重點。"
)

_BRIEF_RE = re.compile(r"## 投資重點彙整.*?\n(.*?)\n---", re.S)


def _done_quarters(storage: ReportStorage, stock: str, language: str) -> list[tuple[int, int]]:
    qs = []
    for e in storage.manifest.values():
        if e.get("status") == "done" and e.get("co_id") == stock and e.get("language") == language:
            qs.append((e["year"], e["quarter"]))
    return sorted(set(qs))


def run_aggregate(cfg: ReportsConfig, stock: str, n_quarters: int = 8) -> int:
    storage = ReportStorage(cfg.data_dir)
    quarters = _done_quarters(storage, stock, cfg.language)[-n_quarters:]
    if not quarters:
        logger.error("找不到 %s 已下載的財報，請先 fetch。", stock)
        return 1
    api_key = llm.get_api_key()
    logger.info("彙集 %s 近 %d 季：%s", stock, len(quarters),
                "、".join(f"{y}Q{q}" for y, q in quarters))

    def _md(year, q):
        return storage.root / "analysis" / f"{stock}_{year}Q{q}_{cfg.language}.md"

    # 平行跑尚未分析的季（各季獨立，彼此不影響）
    todo = [(y, q) for y, q in quarters if not _md(y, q).exists()]
    if todo:
        workers = min(len(todo), max(1, cfg.workers))
        logger.info("平行分析 %d 季（workers=%d）…", len(todo), workers)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(analyze_report, cfg, stock, y, q): (y, q) for y, q in todo}
            for fut in as_completed(futs):
                y, q = futs[fut]
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("  %dQ%d 分析失敗：%s", y, q, exc)

    briefs, name = [], ""
    for year, q in quarters:
        md_path = _md(year, q)
        if not md_path.exists():
            continue
        md = md_path.read_text(encoding="utf-8")
        if not name:
            m = re.search(r"# \S+ (\S+) ", md)
            name = m.group(1) if m else ""
        bm = _BRIEF_RE.search(md)
        if bm:
            briefs.append({"q": f"{year}Q{q}", "brief": bm.group(1).strip()})

    if not briefs:
        logger.error("沒有可彙集的單季重點。")
        return 1

    body = "\n\n".join(f"===== {b['q']} =====\n{b['brief']}" for b in briefs)
    res = llm.chat(cfg.strong_model, AGG_SYSTEM,
                   f"公司：{stock} {name}\n涵蓋季度：{'、'.join(b['q'] for b in briefs)}\n\n{body}", api_key)
    logger.info("  跨季彙整（%s）✓ $%.4f", cfg.strong_model, res.get("cost") or 0)

    q_span = f"{briefs[0]['q']}_to_{briefs[-1]['q']}"
    out = storage.root / "analysis" / f"{stock}_aggregate_{q_span}_{cfg.language}.md"
    lines = [f"# {stock} {name} 近 {len(briefs)} 季財報彙集（{briefs[0]['q']}–{briefs[-1]['q']}）",
             f"<sub>{cfg.strong_model} · 跨季趨勢判讀</sub>", "", res["text"]]
    out.write_text("\n".join(lines), encoding="utf-8")
    logger.info("完成。跨季彙集已寫入 %s", out)
    return 0
