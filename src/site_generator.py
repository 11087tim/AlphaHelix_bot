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


def _linkify_citations(summary: str, references: list[dict]) -> Markup:
    """把摘要內的 [n] 標記換成連到對應推文的可點擊連結，其餘文字做 HTML escape。"""
    url_by_n = {ref["n"]: ref["url"] for ref in references}

    # 逐段處理：非標記文字 escape，標記換成連結。所有片段都是 Markup，join 不會重複 escape。
    parts: list[Markup] = []
    last = 0
    for m in _CITE_RE.finditer(summary):
        parts.append(escape(summary[last:m.start()]))
        n = int(m.group(1))
        url = url_by_n.get(n)
        if url:
            # Markup(...).format 會自動 escape 參數，url 在屬性內、n 在內文都安全
            parts.append(
                Markup('<a class="cite" href="{}" target="_blank" rel="noopener">[{}]</a>').format(url, n)
            )
        else:
            parts.append(escape(m.group(0)))  # 沒有對應連結就原樣顯示
        last = m.end()
    parts.append(escape(summary[last:]))
    return Markup("").join(parts)


def prepare_sections(raw_sections: list[dict]) -> list[dict]:
    """把 summarizer 產出的 section（含 [n] 標記與 references）轉成可直接渲染的資料。"""
    prepared = []
    for sec in raw_sections:
        prepared.append(
            {
                "label": sec["label"],
                "summary_html": _linkify_citations(sec["summary"], sec["references"]),
                "references": sec["references"],
            }
        )
    return prepared


def _prepare_digest(d: dict) -> dict:
    """把儲存的 digest（含 [n] 標記文字）轉成可渲染（已 linkify）的版本。"""
    return {
        "id": d["id"],
        "generated_at": d["generated_at"],
        "account_sections": prepare_sections(d.get("account_sections", [])),
        "keyword_sections": prepare_sections(d.get("keyword_sections", [])),
    }


def render_site(title: str, digests: list[dict], output_dir: Path) -> None:
    """從每小時摘要清單（由新到舊）產生網站 index.html，每個時段為可折疊區塊。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    prepared = [_prepare_digest(d) for d in digests]

    html = _env.get_template("site.html").render(
        title=title,
        updated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        digests=prepared,
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
