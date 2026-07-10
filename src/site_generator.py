from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)

_CITE_RE = re.compile(r"\[(\d+)\]")
_FIG_RE = re.compile(r"\[附圖(\d+)\]")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEADER_RE = re.compile(r"^#{1,6}\s+(.*)")
_BOLD_LINE_RE = re.compile(r"^\*\*(.+?)\*\*[:：]?$")  # 整行都是粗體 → 視為主題小標（靠左）
_BULLET_RE = re.compile(r"^[-*]\s+(.*)")
_ORDERED_RE = re.compile(r"^\d+[.)]\s+(.*)")


def _render_inline(text: str, url_by_n: dict) -> Markup:
    """行內渲染：先整段 escape，再注入 **粗體**、[n] 引用連結、[附圖N] 預覽連結。"""
    out = str(escape(text))  # 先 escape，**、[n]、[附圖N] 都是純字元會保留
    out = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)

    # [附圖N] 先處理（避免被 [n] 規則吃到數字）
    out = _FIG_RE.sub(lambda m: f'<span class="figref" data-fig="{m.group(1)}">[附圖{m.group(1)}]</span>', out)

    def cite(m: re.Match) -> str:
        n = int(m.group(1))
        url = url_by_n.get(n)
        if not url:
            return m.group(0)
        return f'<a class="cite" href="{escape(url)}" target="_blank" rel="noopener">[{n}]</a>'

    out = _CITE_RE.sub(cite, out)
    return Markup(out)


def _render_summary(summary: str, references: list[dict]) -> Markup:
    """把 LLM 產出的 Markdown-lite 摘要（粗體/條列/標題/[n] 引用）渲染成安全的 HTML。"""
    url_by_n = {ref["n"]: ref["url"] for ref in references}
    parts: list[str] = []
    list_items: list[str] = []
    list_tag = ""  # "ul" 或 "ol"

    def flush_list() -> None:
        nonlocal list_tag
        if list_items:
            items = "".join(f"<li>{it}</li>" for it in list_items)
            parts.append(f"<{list_tag}>{items}</{list_tag}>")
            list_items.clear()
            list_tag = ""

    for raw in summary.split("\n"):
        line = raw.strip()
        if not line:
            flush_list()
            continue

        header = _HEADER_RE.match(line)
        if header:
            flush_list()
            parts.append(f'<p class="maintopic">{_render_inline(header.group(1), url_by_n)}</p>')
            continue

        bold_line = _BOLD_LINE_RE.match(line)
        if bold_line:
            flush_list()
            parts.append(f'<p class="subhead">{_render_inline(bold_line.group(1), url_by_n)}</p>')
            continue

        bullet = _BULLET_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if bullet or ordered:
            tag = "ul" if bullet else "ol"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            content = (bullet or ordered).group(1)
            list_items.append(str(_render_inline(content, url_by_n)))
            continue

        flush_list()
        parts.append(f"<p>{_render_inline(line, url_by_n)}</p>")

    flush_list()
    return Markup("".join(parts))


def _prepare_refs(references: list[dict]) -> tuple[list[dict], list[dict]]:
    """回傳 (refs_display, figures)。
    refs_display：每筆來源推文帶自己的縮圖（含 fig_no）；figures：攤平的圖清單，供 lightbox 用。"""
    refs_out, figures, seen = [], [], set()
    for ref in references:
        media = []
        for m in ref.get("media", []):
            if not m.get("image_url"):
                continue
            item = {
                "fig_no": m.get("fig_no"),
                "image_url": m["image_url"],
                "type": m.get("type", "photo"),
                "alt": m.get("alt_text", ""),
            }
            media.append(item)
            if item["fig_no"] and item["fig_no"] not in seen:
                seen.add(item["fig_no"])
                figures.append(item)
        refs_out.append({"n": ref["n"], "url": ref["url"], "author": ref["author"], "media": media})
    figures.sort(key=lambda f: f["fig_no"] or 0)
    return refs_out, figures


def prepare_sections(raw_sections: list[dict]) -> list[dict]:
    """把 summarizer 產出的 section（含 Markdown-lite 摘要與 references）轉成可直接渲染的資料。
    只呈現摘要實際引用到的來源與媒體：被模型忽略（如宣傳/訂閱推銷）的推文不會顯示連結或縮圖。"""
    prepared = []
    for sec in raw_sections:
        summary = sec["summary"]
        cited = {int(m.group(1)) for m in _CITE_RE.finditer(summary)}
        refs = [r for r in sec["references"] if r["n"] in cited]
        refs_display, figures = _prepare_refs(refs)
        prepared.append(
            {
                "label": sec["label"],
                "summary_html": _render_summary(summary, refs),
                "references": refs_display,
                "figures": figures,
            }
        )
    return prepared


def _prepare_digest(d: dict) -> dict:
    """把儲存的 digest（含 [n] 標記文字）轉成可渲染（已 linkify）的版本。"""
    return {
        "id": d["id"],
        "generated_at": d["generated_at"],
        "date": d["generated_at"].split(" ")[0],  # YYYY-MM-DD，供日期篩選用
        "model": d.get("model", ""),
        "account_sections": prepare_sections(d.get("account_sections", [])),
        "keyword_sections": prepare_sections(d.get("keyword_sections", [])),
    }


def render_site(title: str, digests: list[dict], output_dir: Path) -> None:
    """從每小時摘要清單（由新到舊）產生網站 index.html，每個時段為可折疊區塊。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    prepared = [_prepare_digest(d) for d in digests]

    # 可選日期清單，由新到舊、去重
    dates: list[str] = []
    for d in prepared:
        if d["date"] not in dates:
            dates.append(d["date"])

    html = _env.get_template("site.html").render(
        title=title,
        updated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        digests=prepared,
        dates=dates,
    )
    (output_dir / "index.html").write_text(html, encoding="utf-8")
    logger.info("已更新網站：%s（%d 個時段）", output_dir / "index.html", len(prepared))


def render_email(title: str, digests: list[dict], site_url: str = "") -> str:
    """從待寄的每小時摘要清單（由新到舊）產生 email HTML（攤平、不折疊）。"""
    prepared = [_prepare_digest(d) for d in digests]
    if prepared:
        range_label = f"{prepared[-1]['generated_at']} ～ {prepared[0]['generated_at']}"
    else:
        range_label = ""
    return _env.get_template("email.html").render(
        title=title,
        digests=prepared,
        range_label=range_label,
        site_url=site_url,
    )
