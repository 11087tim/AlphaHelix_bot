from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _list_archive_links(archive_dir: Path) -> list[dict]:
    if not archive_dir.exists():
        return []
    files = sorted(archive_dir.glob("*.html"), reverse=True)
    links = []
    for f in files:
        links.append({"href": f"archive/{f.name}", "label": f.stem})
    return links


def render_digest(
    title: str,
    account_sections: list[dict],
    keyword_sections: list[dict],
    output_dir: Path,
) -> str:
    """產生一份 digest：寫入 archive 存檔並更新首頁 index.html。回傳首頁 HTML 內容（供寄信重用）。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = output_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    generated_at = now.strftime("%Y-%m-%d %H:%M")
    stamp = now.strftime("%Y%m%d-%H%M")

    template = _env.get_template("digest.html")

    # 先寫 archive 存檔（不含歷史列表）
    archive_html = template.render(
        title=f"{title}（{generated_at}）",
        generated_at=generated_at,
        account_sections=account_sections,
        keyword_sections=keyword_sections,
        archive_links=[],
    )
    archive_file = archive_dir / f"{stamp}.html"
    archive_file.write_text(archive_html, encoding="utf-8")

    # 再產生首頁（含歷史列表），archive 已包含這次存檔
    index_html = template.render(
        title=title,
        generated_at=generated_at,
        account_sections=account_sections,
        keyword_sections=keyword_sections,
        archive_links=_list_archive_links(archive_dir),
    )
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")

    logger.info("已產生網站：%s", output_dir / "index.html")
    return index_html
