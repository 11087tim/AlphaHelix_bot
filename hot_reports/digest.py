"""研報 PDF → 文字 → LLM 單篇摘要 + 跨篇彙整（走 OpenRouter，沿用 reports.llm）。"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from reports import llm

from . import config

logger = logging.getLogger(__name__)

SUMMARY_SYSTEM = (
    "你是投資研究助理。你會收到一份外資機構研究報告的全文文字（可能有 OCR/排版雜訊）。"
    "用繁體中文輸出精煉摘要，格式：\n"
    "**核心論點**（1-3 句）\n"
    "- 關鍵事實/數據要點（每點一行，保留具體數字，最多 8 點）\n"
    "- 投資結論/評級/目標價（報告有寫才列，不要編造）\n"
    "只輸出摘要本身，不要開場白。全文若被截斷，就摘要看得到的部分。"
)

SYNTH_SYSTEM = (
    "你是投資研究主管。你會收到今日多份外資研報的摘要（每份標注機構與標題）。"
    "用繁體中文寫一份跨報告彙整：\n"
    "## 今日主線\n先用 3-5 句話講今日這批報告合起來反映的市場焦點與方向。\n"
    "## 主題彙整\n依主題分組（用 ### 子標題），比較不同機構的觀點異同，個股觀點單獨列出。\n"
    "## 值得追蹤\n列出 2-4 個後續值得驗證的判斷或催化劑。\n"
    "只根據輸入的摘要內容，不要編造；不同機構觀點衝突時明確指出。"
)


def extract_text(pdf_path: Path) -> str:
    from pypdf import PdfReader
    try:
        reader = PdfReader(pdf_path)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        logger.warning("PDF 抽文字失敗 %s：%s", pdf_path.name, exc)
        return ""
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def summarize_report(meta: dict, text: str, api_key: str) -> str:
    header = (f"機構：{meta.get('nash_securities') or meta.get('institution')}\n"
              f"標題：{meta.get('nash_title') or meta.get('title_en')}\n"
              f"日期：{meta.get('nash_date') or meta.get('date')}｜"
              f"頁數：{meta.get('nash_pages') or meta.get('pages')}\n\n")
    body = text[:config.MAX_TEXT_CHARS]
    res = llm.chat(config.SUMMARY_MODEL, SUMMARY_SYSTEM, header + body, api_key, timeout=300)
    logger.info("單篇摘要完成（%s，cost=%s）", meta.get('nash_title', '')[:40], res.get('cost'))
    return res["text"]


def synthesize(summaries: list[dict], api_key: str) -> str:
    """summaries: [{title, securities, summary}]"""
    blocks = []
    for i, s in enumerate(summaries, 1):
        blocks.append(f"[{i}] {s['securities']}｜{s['title']}\n{s['summary']}")
    res = llm.chat(config.SYNTH_MODEL, SYNTH_SYSTEM, "\n\n---\n\n".join(blocks), api_key, timeout=300)
    logger.info("跨篇彙整完成（%d 篇，cost=%s）", len(summaries), res.get('cost'))
    return res["text"]
