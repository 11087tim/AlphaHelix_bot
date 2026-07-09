from __future__ import annotations

import base64
import logging
import re

from .config import ReportsConfig
from . import llm, router, twse
from .storage import ReportStorage

logger = logging.getLogger(__name__)

_TOC_RE = re.compile(r"[（(]([一二三四五六七八九十百]+)[)）]([^\d]+?)(\d+)(?:～(\d+))?")

SYNTH_SYSTEM = (
    "你是資深投資分析師。以下是某台股公司財報附註各節的擷取結果。"
    "請據此寫一份精簡、有判斷力的『投資重點彙整』，要有洞見、不要只是複述數字：\n"
    "1. 本份財報最值得注意的 3～5 個重點（附具體數字）。\n"
    "2. 跨項目的連結判讀（例如：資本支出承諾 vs 存貨 vs 分部營收 → 擴產是否合理；毛利/研發/客戶集中度的訊號）。\n"
    "3. 潛在風險或紅旗（重大訴訟、關係人異常、或有負債、客戶過度集中、質押偏高等）。\n"
    "4. 最後一行用「一句話：」總結對投資的意涵。\n"
    "只根據提供的內容判讀，不要杜撰數字。繁體中文，用小標＋條列，精簡。"
)

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


def _find_note(notes: list[dict], title: str) -> dict | None:
    """把路由器給的 note_title 對應到目錄實際的節。"""
    tn = _norm(title)
    for note in notes:
        nt = _norm(note["title"])
        if nt and (nt in tn or tn in nt):
            return note
    best, score = None, 0
    for note in notes:
        nt = _norm(note["title"])
        s = sum(1 for i in range(len(tn) - 1) if tn[i:i + 2] in nt)
        if s > score:
            best, score = note, s
    return best if score >= 2 else None


def _synthesize(cfg: ReportsConfig, stock: str, name: str, industry: str,
                sections: list[dict], api_key: str) -> tuple[str, float]:
    body = "\n\n".join(f"【{s['title']}】\n{s['text']}" for s in sections)
    user = f"公司：{stock} {name}（產業：{industry}）\n\n各附註擷取結果：\n\n{body}"
    res = llm.chat(cfg.strong_model, SYNTH_SYSTEM, user, api_key)
    return res["text"], res.get("cost") or 0


def _fixed_plan(cfg: ReportsConfig, notes: list[dict]) -> list[dict]:
    """路由失敗時的回退：用固定主題清單。"""
    plan = []
    for t in cfg.analysis_topics:
        note = _match_note(notes, t["match"])
        if note:
            plan.append({"note_title": note["title"], "focus": t["instruction"],
                         "mode": "table" if t["mode"] == "vision" else "narrative"})
    return plan


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

    info = twse.get_company_info(stock)
    name, industry = info.get("name", ""), info.get("industry", "")

    # ③ 自適應層：Opus 讀「目錄+產業」決定這家公司要看什麼
    plan, router_cost = router.plan_extraction(cfg, stock, name, industry, notes, api_key)
    used_router = bool(plan)
    if not plan:
        plan = _fixed_plan(cfg, notes)
    logger.info("%s %s（%s）：附註 %d 節、位移 %d｜%s 產出 %d 個擷取主題（路由成本 $%.4f）",
                stock, name, industry, len(notes), offset,
                "Opus 路由" if used_router else "固定回退", len(plan), router_cost)

    header = [f"# {stock} {name} {year}Q{quarter} 財報附註分析",
              f"<sub>產業：{industry}｜擷取計畫由 {'Opus 自適應路由' if used_router else '固定主題'} 產生</sub>", ""]
    detail_lines: list[str] = []
    sections: list[dict] = []
    total_cost = router_cost

    for item in plan:
        note = _find_note(notes, item.get("note_title", ""))
        if not note:
            logger.info("  「%s」：目錄找不到對應附註，略過", item.get("note_title"))
            continue
        mode = "vision" if item.get("mode") == "table" else "text"
        pdf_pages = list(range(note["start"] + offset, note["end"] + offset + 1))[: cfg.vision_max_pages]
        user = (f"【要擷取的重點】{item.get('focus', '')}\n"
                f"（本節為附註「{note['title']}」，印刷頁 {note['start']}–{note['end']}）")

        if mode == "vision":
            res = llm.vision_chat(cfg.vision_model, EXTRACT_SYSTEM, user, _render_pages(doc, pdf_pages), api_key)
            model_used = cfg.vision_model
        else:
            text = _pages_text(doc, pdf_pages)[: cfg.chunk_chars * 6]
            res = llm.chat(cfg.cheap_model, EXTRACT_SYSTEM, user + "\n\n【內容】\n" + text, api_key)
            model_used = cfg.cheap_model

        total_cost += res.get("cost") or 0
        logger.info("  「%s」→ 附註「%s」（%s, %d 頁）✓", note["title"], note["title"], mode, len(pdf_pages))
        sections.append({"title": note["title"], "text": res["text"]})
        detail_lines += [f"## {note['title']}",
                         f"<sub>印刷頁 {note['start']}–{note['end']}｜{mode}／{model_used}｜重點：{item.get('focus','')}</sub>",
                         "", res["text"], ""]

    # 最終彙整層：Opus 讀各節擷取結果 → 一頁投資 brief（跨項目洞察、風險紅旗）
    brief_lines: list[str] = []
    if cfg.synthesis_enabled and sections:
        brief, bcost = _synthesize(cfg, stock, name, industry, sections, api_key)
        total_cost += bcost
        logger.info("  最終彙整（%s）✓ $%.4f", cfg.strong_model, bcost)
        brief_lines = ["## 投資重點彙整",
                       f"<sub>{cfg.strong_model} · 跨附註判讀</sub>", "", brief, "",
                       "---", "", "# 各附註擷取明細", ""]

    out = storage.root / "analysis" / f"{stock}_{year}Q{quarter}_{cfg.language}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(header + brief_lines + detail_lines), encoding="utf-8")
    logger.info("完成。總成本 $%.4f，分析已寫入 %s", total_cost, out)
    return 0
