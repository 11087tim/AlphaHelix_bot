from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
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


def _render_inline(text: str, cite_by_n: dict) -> Markup:
    """行內渲染：先整段 escape，再注入 **粗體**、[n] 引用連結（hover 顯示來源）、[附圖N] 預覽連結。"""
    out = str(escape(text))  # 先 escape，**、[n]、[附圖N] 都是純字元會保留
    out = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)

    # [附圖N] 先處理（避免被 [n] 規則吃到數字）
    out = _FIG_RE.sub(lambda m: f'<span class="figref" data-fig="{m.group(1)}">[附圖{m.group(1)}]</span>', out)

    def cite(m: re.Match) -> str:
        n = int(m.group(1))
        info = cite_by_n.get(n)
        if not info:
            return m.group(0)
        title = f' title="{escape(info["title"])}"' if info.get("title") else ""
        return (f'<a class="cite" href="{escape(info["url"])}"{title} '
                f'target="_blank" rel="noopener">[{n}]</a>')

    out = _CITE_RE.sub(cite, out)
    return Markup(out)


def _render_lines(lines: list[str], cite_by_n: dict) -> str:
    """把一段 Markdown-lite 文字（粗體/條列/標題/[n] 引用）渲染成安全的 HTML 片段。"""
    parts: list[str] = []
    list_items: list[str] = []
    list_tag = ""  # "ul" 或 "ol"

    def flush_list() -> None:
        nonlocal list_tag
        if list_items:
            items = "".join(f"<li>{it}</li>" for it in list_items)
            cls = ' class="robot-list"' if robot_open[0] else ""
            parts.append(f"<{list_tag}{cls}>{items}</{list_tag}>")
            list_items.clear()
            list_tag = ""
            robot_open[0] = False  # 列點收進 callout 後即關閉

    robot_open = [False]  # 🤖 標記後、下一組列點歸入 callout
    for raw in lines:
        line = raw.strip()
        if not line:
            flush_list()
            continue

        header = _HEADER_RE.match(line)
        if header:
            flush_list()
            robot_open[0] = False
            parts.append(f'<p class="maintopic">{_render_inline(header.group(1), cite_by_n)}</p>')
            continue

        bold_line = _BOLD_LINE_RE.match(line)
        if bold_line:
            flush_list()
            robot_open[0] = False
            parts.append(f'<p class="subhead">{_render_inline(bold_line.group(1), cite_by_n)}</p>')
            continue

        bullet = _BULLET_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if bullet or ordered:
            tag = "ul" if bullet else "ol"
            if list_tag and list_tag != tag:
                flush_list()
            list_tag = tag
            content = (bullet or ordered).group(1)
            list_items.append(str(_render_inline(content, cite_by_n)))
            continue

        flush_list()
        is_robot = line.startswith("🤖")
        robot_open[0] = is_robot  # 🤖 標記後接的列點要包進同一個 callout（robot-list）
        cls = ' class="robot"' if is_robot else ""  # 🤖 延伸推論做成 callout
        parts.append(f"<p{cls}>{_render_inline(line, cite_by_n)}</p>")

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


def _cite_title(ref: dict) -> str:
    """[n] 的 hover 提示：來源作者 + 內文摘要（舊 digest 無 text 時只顯示作者）。"""
    author = str(ref.get("author", "")).strip()
    disp = author if author.startswith("🎙️") else f"@{author}"
    text = str(ref.get("text", "")).strip()
    title = f"{disp}：{text}" if text else disp
    return title[:220]


def _render_summary(summary: str, references: list[dict]) -> Markup:
    """渲染摘要 HTML；只呈現內文以 [附圖N] 明確引用到的圖（模型確實有討論到內容者），
    放在該大/小主題段落（延伸推論）下方。單純被 [n] 引用、模型未提及內容的附圖不顯示，
    避免倒出與內文無關的縮圖（來源清單仍保留該推文連結）。"""
    cite_by_n = {ref["n"]: {"url": ref["url"], "title": _cite_title(ref)} for ref in references}
    fig_by_no = _fig_map(references)

    out: list[str] = []
    for seg in _split_segments(summary):
        block = _render_lines(seg, cite_by_n)
        text = "\n".join(seg)
        figs: dict[int, dict] = {}
        for m in _FIG_RE.finditer(text):  # 只挑內文明確 [附圖N] 提到的圖
            fn = int(m.group(1))
            if fn in fig_by_no:
                figs[fn] = fig_by_no[fn]
        if figs:
            block += _figs_html(sorted(figs.values(), key=lambda f: f["fig_no"] or 0))
        head = next((ln.strip() for ln in seg if ln.strip()), "")
        if head.startswith("#") and "本期變化" in head:  # 📈 本期變化整段包成醒目卡片
            block = f'<div class="whatschanged">{block}</div>'
        out.append(block)
    return Markup("".join(out))


def _compress_ns(ns: list[int]) -> str:
    """把引用編號壓成區間字串，如 [1,2,3,5] → '1–3, 5'。"""
    ns = sorted(set(ns))
    parts, i = [], 0
    while i < len(ns):
        j = i
        while j + 1 < len(ns) and ns[j + 1] == ns[j] + 1:
            j += 1
        parts.append(str(ns[i]) if i == j else f"{ns[i]}–{ns[j]}")
        i = j + 1
    return ", ".join(parts)


def prepare_sections(raw_sections: list[dict]) -> list[dict]:
    """把 summarizer 產出的 section（含 Markdown-lite 摘要與 references）轉成可直接渲染的資料。
    附圖跟著各主題段落走；底部來源清單【依連結去重】——同一集 podcast 只列一次、編號收合成 [1–N]，
    推文因每則連結不同維持逐則。只呈現摘要實際引用到的來源。"""
    prepared = []
    for sec in raw_sections:
        summary = sec["summary"]
        cited = {int(m.group(1)) for m in _CITE_RE.finditer(summary)}
        refs = [r for r in sec["references"] if r["n"] in cited]
        # 依連結分組：同一 URL（如同一集）只顯示一列，收合其所有引用編號
        grouped: dict[str, dict] = {}
        order: list[str] = []
        for r in refs:
            key = r["url"]
            if key not in grouped:
                grouped[key] = {"url": r["url"], "author": r["author"], "ns": []}
                order.append(key)
            grouped[key]["ns"].append(r["n"])
        refs_display = [{"url": grouped[k]["url"], "author": grouped[k]["author"],
                         "ns_label": _compress_ns(grouped[k]["ns"])} for k in order]
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


def render_email(title: str, digests: list[dict], site_url: str = "",
                 window_hours: float = 0) -> str:
    """產生 email HTML（攤平、不折疊）。涵蓋時段以「起（最早涵蓋起點）～ 迄（最新產生時間）」呈現。"""
    prepared = [_prepare_digest(d) for d in digests]
    prepared.sort(key=lambda d: d["generated_at"])  # 由舊到新
    range_label = ""
    if prepared:
        end = prepared[-1]["generated_at"]
        start = prepared[0]["generated_at"]
        if window_hours:  # 起點回推抓取時間窗，代表這份涵蓋的活動起點
            try:
                start = (datetime.strptime(start, "%Y-%m-%d %H:%M")
                         - timedelta(hours=window_hours)).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
        range_label = end if start == end else f"{start} ～ {end}"
    return _env.get_template("email.html").render(
        title=title,
        digests=list(reversed(prepared)),  # 顯示由新到舊
        range_label=range_label,
        site_url=site_url,
    )
