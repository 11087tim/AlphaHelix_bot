from __future__ import annotations

import logging
from pathlib import Path

from .config import ReportsConfig
from .storage import ReportStorage

logger = logging.getLogger(__name__)


def _extract_text(pdf_path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            parts.append("")
    return "\n".join(parts)


def run_extract(cfg: ReportsConfig) -> int:
    """把已下載的 PDF 轉成純文字（供日後 LLM 使用）。只處理尚未轉過的。"""
    storage = ReportStorage(cfg.data_dir)
    entries = storage.done_pdfs()
    if not entries:
        logger.info("沒有已下載的 PDF 可轉文字。")
        return 0

    done = skipped = failed = 0
    for e in entries:
        pdf_path = storage.root / e["pdf_path"]
        lang = e.get("language", "zh")
        txt_path = storage.text_dir / f"{e['co_id']}" / f"{e['year']}Q{e['quarter']}_{e['report_type']}_{lang}.txt"
        if txt_path.exists():
            skipped += 1
            continue
        try:
            text = _extract_text(pdf_path)
            txt_path.parent.mkdir(parents=True, exist_ok=True)
            txt_path.write_text(text, encoding="utf-8")
            done += 1
            logger.info("轉文字 ✓ %s（%d 字）", txt_path.name, len(text))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("轉文字失敗 %s：%s", pdf_path, exc)

    logger.info("完成。新轉 %d 份、已存在略過 %d 份、失敗 %d 份。", done, skipped, failed)
    return 0
