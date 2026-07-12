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


def _render_lines(lines: list[str], url_by_n: dict) -> str:
    """把一段 Markdown-lite 文字（粗體/條列/標題/[n] 引用）渲染成安全的 HTML 片段。"""
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

    for raw in lines:
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
    return "".join(parts)


def _fig_map(references: list[dict]) -> dict:
    """建立 fig_no→media 對照，供內文 [附圖N] 顯示縮圖。"""
    fig_by_no: dict[int, dict] = {}
    for ref in references:
        for m in ref.get("media", []):
            if not m.get("image_url") or not m.get("fig_no"):
                continue
            fig_by_no[m["fig_no"]] = {
                "fig_no": m["fig_no"],
                "image_url": m["image_url"],
                "type": m.get("type", "photo"),
                "alt": m.get("alt_text", ""),
            }
    return fig_by_no


def _figs_html(items: list[dict]) -> str:
    """把一組附圖渲染成一列縮圖（點擊開 lightbox 預覽，不連到 X）。"""
    cells = []
    for m in items:
        alt = escape(m["alt"] or f"附圖{m['fig_no']}")
        play = '<span class="play">▶</span>' if m["type"] != "photo" else ""
        cells.append(
            f'<a class="thumb" href="{escape(m["image_url"])}" data-fig="{m["fig_no"]}" '
            f'data-type="{escape(m["type"])}" title="附圖{m["fig_no"]}">'
            f'<img src="{escape(m["image_url"])}" alt="{alt}" loading="lazy">'
            f'<span class="figno">附圖{m["fig_no"]}</span>{play}</a>'
        )
    return '<div class="secfigs">' + "".join(cells) + "</div>"


def _split_segments(summary: str) -> list[list[str]]:
    """依大主題 `## ` 或子主題 `**標題**` 標題切段，讓每段可各自附上引用到的附圖。"""
    segments: list[list[str]] = []
    cur: list[str] = []
    for raw in summary.split("\n"):
        line = raw.strip()
        is_head = _HEADER_RE.match(line) or _BOLD_LINE_RE.match(line)
        if is_head and any(x.strip() for x in cur):
            segments.append(cur)
            cur = []
        cur.append(raw)
    if cur:
        segments.append(cur)
    return segments


def _render_summary(summary: str, references: list[dict]) -> Markup:
    """渲染摘要 HTML；只呈現內文以 [附圖N] 明確引用到的圖（模型確實有討論到內容者），
    放在該大/小主題段落（延伸推論）下方。單純被 [n] 引用、模型未提及內容的附圖不顯示，
    避免倒出與內文無關的縮圖（來源清單仍保留該推文連結）。"""
    url_by_n = {ref["n"]: ref["url"] for ref in references}
    fig_by_no = _fig_map(references)

    out: list[str] = []
    for seg in _split_segments(summary):
        out.append(_render_lines(seg, url_by_n))
        text = "\n".join(seg)
        figs: dict[int, dict] = {}
        for m in _FIG_RE.finditer(text):  # 只挑內文明確 [附圖N] 提到的圖
            fn = int(m.group(1))
            if fn in fig_by_no:
                figs[fn] = fig_by_no[fn]
        if figs:
            out.append(_figs_html(sorted(figs.values(), key=lambda f: f["fig_no"] or 0)))
    return Markup("".join(out))


def prepare_sections(raw_sections: list[dict]) -> list[dict]:
    """把 summarizer 產出的 section（含 Markdown-lite 摘要與 references）轉成可直接渲染的資料。
    附圖跟著各主題段落走（放在段落下方）；底部只留純文字來源清單 [n]。
    只呈現摘要實際引用到的來源：被模型忽略（如宣傳/訂閱推銷）的推文不會顯示。"""
    prepared = []
    for sec in raw_sections:
        summary = sec["summary"]
        cited = {int(m.group(1)) for m in _CITE_RE.finditer(summary)}
        refs = [r for r in sec["references"] if r["n"] in cited]
        refs_display = [{"n": r["n"], "url": r["url"], "author": r["author"]} for r in refs]
        prepared.append(
            {
                "label": sec["label"],
                "summary_html": _render_summary(summary, refs),
                "references": refs_display,
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
        "podcast_sections": prepare_sections(d.get("podcast_sections", [])),
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
    if not prepared:
        range_label = ""
    elif prepared[0]["generated_at"] == prepared[-1]["generated_at"]:
        range_label = prepared[0]["generated_at"]  # 單一時段就不顯示頭尾相同的區間
    else:
        range_label = f"{prepared[-1]['generated_at']} ～ {prepared[0]['generated_at']}"
    return _env.get_template("email.html").render(
        title=title,
        digests=prepared,
        range_label=range_label,
        site_url=site_url,
    )
