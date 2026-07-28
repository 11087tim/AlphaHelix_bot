"""把每日研報彙整（digest/*.md）渲染成網站分頁 docs/hot_reports.html。

風格對齊主站（淺/深色自動、卡片式），每天一個可摺疊區塊，最新在上。
"""
from __future__ import annotations

import html
import logging
import re
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

MAX_DAYS = 30   # 頁面保留最近 30 天

_PAGE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>熱門外資研報</title>
<style>
  :root { --bg:#f7f9fb; --card:#fff; --fg:#1c2733; --muted:#5b6b7b; --line:#e3e9f0; --accent:#2f6fab; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0f1620; --card:#16202b; --fg:#dbe6f1; --muted:#8fa2b5; --line:#274156; --accent:#7cc0f0; }
  }
  * { box-sizing:border-box; }
  body { margin:0; padding:24px 16px 60px; background:var(--bg); color:var(--fg);
         font:16px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",sans-serif; }
  .wrap { max-width:860px; margin:0 auto; }
  h1 { font-size:1.5rem; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:.88rem; margin:0 0 20px; }
  .navlinks a { display:inline-block; padding:6px 14px; border-radius:999px; font-size:.9rem;
    color:var(--accent); background:var(--card); border:1px solid var(--line); text-decoration:none; margin-right:8px; }
  details.day { background:var(--card); border:1px solid var(--line); border-radius:14px;
    margin:14px 0; padding:4px 20px; }
  details.day > summary { cursor:pointer; font-weight:700; font-size:1.05rem; padding:12px 0; list-style:none; }
  details.day > summary::before { content:"▸ "; color:var(--accent); }
  details.day[open] > summary::before { content:"▾ "; }
  h2 { font-size:1.15rem; margin:20px 0 8px; color:var(--accent); }
  h3 { font-size:1rem; margin:16px 0 6px; }
  .report { border-top:1px solid var(--line); margin-top:18px; padding-top:6px; }
  .report h2 { font-size:1rem; color:var(--fg); }
  ul { margin:6px 0 12px; padding-left:22px; }
  li { margin:2px 0; }
  hr { border:none; border-top:1px solid var(--line); margin:18px 0; }
  .foot { color:var(--muted); font-size:.8rem; margin-top:30px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>📑 熱門外資研報</h1>
  <p class="sub">每日自動彙整 valuelist 熱榜研報（LLM 摘要，非原文）。最新 __N__ 天。</p>
  <p class="navlinks"><a href="index.html">🏠 每日 X 摘要</a><a href="leverage.html">📊 台股槓桿儀表板</a></p>
__DAYS__
  <p class="foot">內容由 LLM 自動生成，僅供研究參考，非投資建議。</p>
</div>
</body>
</html>
"""


def _md_block(md: str) -> str:
    """digest markdown → HTML（標題/粗體/列點/分隔線）。"""
    out_lines: list[str] = []
    in_list = False

    def close_list():
        nonlocal in_list
        if in_list:
            out_lines.append("</ul>")
            in_list = False

    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            close_list()
            continue
        if line == "---":
            close_list()
            out_lines.append("<hr>")
            continue
        m = re.match(r'^(#{1,3})\s+(.*)', line)
        if m:
            close_list()
            lvl = min(len(m.group(1)) + 1, 3)   # md 的 # → h2、## → h3
            text = html.escape(m.group(2))
            out_lines.append(f"<h{lvl}>{text}</h{lvl}>")
            continue
        text = html.escape(line)
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
        if text.startswith("- "):
            if not in_list:
                out_lines.append("<ul>")
                in_list = True
            out_lines.append(f"<li>{text[2:]}</li>")
        else:
            close_list()
            out_lines.append(f"<p>{text}</p>")
    close_list()
    return "\n".join(out_lines)


def render_page(output_path: Path) -> bool:
    """從 digest/*.md 生成研報分頁；沒有任何 digest 時回傳 False。"""
    files = sorted(config.DIGEST_DIR.glob("*.md"), reverse=True)[:MAX_DAYS]
    if not files:
        return False
    blocks = []
    for i, f in enumerate(files):
        day = f.stem
        md = f.read_text(encoding="utf-8")
        # 第一行的「# 日期 熱門研報彙整」標題移除（summary 已顯示日期）
        md = re.sub(r'^#\s.*\n', '', md, count=1)
        open_attr = " open" if i == 0 else ""
        blocks.append(f'<details class="day"{open_attr}><summary>{html.escape(day)}</summary>\n'
                      f'{_md_block(md)}\n</details>')
    page = (_PAGE.replace("__N__", str(len(files)))
                 .replace("__DAYS__", "\n".join(blocks)))
    output_path.write_text(page, encoding="utf-8")
    logger.info("研報分頁已生成：%s（%d 天）", output_path, len(files))
    return True
