from __future__ import annotations

import base64
import logging
import re

from .config import ReportsConfig
from . import llm
from .storage import ReportStorage

logger = logging.getLogger(__name__)

_TOC_RE = re.compile(r"[（(]([一二三四五六七八九十百]+)[)）]([^\d]+?)(\d+)(?:～(\d+))?")

EXTRACT_SYSTEM = (
    "你是專業的財報分析師。以下是台股公司財報「附註」中某一節的內容。"
    "請用繁體中文，依指示整理出對投資有用的重點，保留所有具體數字（金額、比率、年增減）。"
    "只根據內容陳述，不要杜撰；若某資訊不在內容中就不要提。輸出用條列/小標，精簡但不遺漏關鍵數字。"
)


def _norm(t: str) -> str:
    return "".join(t.split())


def parse_toc_notes(doc, toc_page: int = 1) -> list[dict]:
    """從目錄頁解析附註各節：回傳 [{title, start, end(印刷頁)}]。"""
    seg = _norm(doc[toc_page].get_text())
    seg = seg.split("附註", 1)[-1]
    notes = []
    for _num, title, start, end in _TOC_RE.findall(seg):
        s = int(start)
        notes.append({"title": title.strip(), "start": s, "end": int(end) if end else s})
    return notes


def detect_offset(doc, notes: list[dict]) -> int:
    """用第一個能定位到的附註標題，推「印刷頁 → PDF 頁」的位移（通常 -1）。"""
    for note in notes:
        kw = note["title"][:4]
        for i in range(len(doc)):
            if i != 1 and kw in _norm(doc[i].get_text()):
                return i - note["start"]
    return -1


def _match_note(notes: list[dict], keywords: list[str]) -> dict | None:
    for note in notes:
        if any(k in note["title"] for k in keywords):
            return note
    return None


def _render_pages(doc, pdf_pages: list[int], zoom: float = 2.2) -> list[str]:
    import fitz

    imgs = []
    for p in pdf_pages:
        if 0 <= p < len(doc):
            png = doc[p].get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")
            imgs.append(base64.b64encode(png).decode())
    return imgs


def _pages_text(doc, pdf_pages: list[int]) -> str:
    return "\n".join(doc[p].get_text() for p in pdf_pages if 0 <= p < len(doc))


def analyze_report(cfg: ReportsConfig, stock: str, year: int, quarter: int,
                   report_type: str = "consolidated") -> int:
    import fitz

    storage = ReportStorage(cfg.data_dir)
    pdf_path = storage.raw_dir / stock / f"{year}Q{quarter}_{report_type}_{cfg.language}.pdf"
    if not pdf_path.exists():
        logger.error("找不到 PDF：%s（請先 fetch）", pdf_path)
        return 1

    doc = fitz.open(str(pdf_path))
    notes = parse_toc_notes(doc)
    offset = detect_offset(doc, notes)
    api_key = llm.get_api_key()
    logger.info("%s %dQ%d：附註 %d 節，頁位移 %d，分析 %d 個主題",
                stock, year, quarter, len(notes), offset, len(cfg.analysis_topics))

    out_lines = [f"# {stock} {year}Q{quarter} 財報附註分析（{report_type}/{cfg.language}）", ""]
    total_cost = 0.0

    for topic in cfg.analysis_topics:
        note = _match_note(notes, topic["match"])
        if not note:
            logger.info("  主題「%s」：目錄找不到對應附註，略過", topic["name"])
            continue
        start_pdf = note["start"] + offset
        end_pdf = note["end"] + offset
        pdf_pages = list(range(start_pdf, end_pdf + 1))[: cfg.vision_max_pages]
        user = f"【指示】{topic['instruction']}\n（本節為附註「{note['title']}」，印刷頁 {note['start']}–{note['end']}）"

        if topic["mode"] == "vision":
            imgs = _render_pages(doc, pdf_pages)
            res = llm.vision_chat(cfg.vision_model, EXTRACT_SYSTEM, user, imgs, api_key)
            model_used = cfg.vision_model
        else:
            text = _pages_text(doc, pdf_pages)[: cfg.chunk_chars * 6]
            res = llm.chat(cfg.cheap_model, EXTRACT_SYSTEM, user + "\n\n【內容】\n" + text, api_key)
            model_used = cfg.cheap_model

        cost = res.get("cost") or 0
        total_cost += cost
        logger.info("  主題「%s」（%s, %d 頁, %s）✓ $%.4f",
                    topic["name"], topic["mode"], len(pdf_pages), model_used, cost)
        out_lines += [f"## {topic['name']}",
                      f"<sub>附註「{note['title']}」印刷頁 {note['start']}–{note['end']}｜{topic['mode']}／{model_used}</sub>",
                      "", res["text"], ""]

    out = storage.root / "analysis" / f"{stock}_{year}Q{quarter}_{cfg.language}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(out_lines), encoding="utf-8")
    logger.info("完成。總成本 $%.4f，分析已寫入 %s", total_cost, out)
    return 0
