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
  h4 { font-size:.95rem; margin:14px 0 4px; }
  .report { border-top:1px solid var(--line); margin-top:18px; padding-top:6px; }
  .report h2 { font-size:1rem; color:var(--fg); }
  ul { margin:6px 0 12px; padding-left:22px; }
  li { margin:2px 0; }
  hr { border:none; border-top:1px solid var(--line); margin:18px 0; }
  .foot { color:var(--muted); font-size:.8rem; margin-top:30px; }
  /* 大綱側欄（設計對齊主站 X digest）*/
  .toc { position:fixed; top:24px; left:24px; width:230px; max-height:calc(100vh - 48px);
    display:flex; flex-direction:column; overflow:hidden; background:var(--card);
    border:1px solid var(--line); border-radius:10px; padding:12px 14px; font-size:.85rem;
    box-shadow:0 2px 8px rgba(0,0,0,.06); }
  .toc-title { font-weight:700; color:var(--accent); margin-bottom:8px; font-size:.9rem; flex:0 0 auto; }
  .toc ul { list-style:none; margin:0; padding:0; overflow-y:auto; flex:1 1 auto; }
  .toc li { margin:1px 0; }
  .toc a { display:block; color:var(--fg); text-decoration:none; padding:3px 6px; border-radius:6px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .toc a:hover { background:var(--bg); color:var(--accent); }
  .toc a.lv1 { font-weight:700; color:var(--accent); margin-top:6px; }
  .toc a.lv2 { padding-left:16px; color:var(--muted); font-size:.82rem; }
  .toc .empty { color:var(--muted); }
  .toc-collapse { float:right; cursor:pointer; border:none; background:transparent;
    color:var(--muted); font-size:.95rem; line-height:1; padding:0 2px; }
  .toc-collapse:hover { color:var(--accent); }
  #tocToggle { display:none; position:fixed; top:18px; left:18px; z-index:20; background:var(--accent);
    color:#fff; border:none; border-radius:8px; padding:8px 12px; font-size:.9rem; cursor:pointer;
    box-shadow:0 2px 8px rgba(0,0,0,.15); }
  body.toc-closed .toc { display:none; }
  body.toc-closed #tocToggle { display:block; }
  @media (max-width:1140px) {
    #tocToggle { top:12px; right:12px; left:auto; padding:6px 10px; font-size:.85rem; }
    .toc { top:54px; left:12px; right:12px; width:auto; max-height:68vh; box-shadow:0 8px 28px rgba(0,0,0,.25); }
  }
</style>
</head>
<body>
<button id="tocToggle" type="button">☰ 大綱</button>
<nav class="toc" id="toc" aria-label="大綱">
  <div class="toc-title">大綱 <button class="toc-collapse" id="tocCollapse" type="button" title="收起大綱" aria-label="收起大綱">✕</button></div>
  <ul id="tocList"></ul>
</nav>
<div class="wrap">
  <h1>📑 熱門外資研報</h1>
  <p class="sub">每日自動彙整 valuelist 熱榜研報（LLM 摘要，非原文）。最新 __N__ 天。</p>
  <p class="navlinks"><a href="index.html">🏠 每日 X 摘要</a><a href="leverage.html">📊 台股槓桿儀表板</a></p>
__DAYS__
  <p class="foot">內容由 LLM 自動生成，僅供研究參考，非投資建議。</p>
</div>
<script>
(function () {
  var days = [].slice.call(document.querySelectorAll('details.day'));
  var tocList = document.getElementById('tocList');
  var toggle = document.getElementById('tocToggle');
  var collapseBtn = document.getElementById('tocCollapse');
  var body = document.body;
  var uid = 0;

  // 側欄收起/展開，並記住偏好（與主站行為一致）
  function setClosed(closed) {
    body.classList.toggle('toc-closed', closed);
    try { localStorage.setItem('hrTocClosed', closed ? '1' : '0'); } catch (e) {}
  }
  if (collapseBtn) collapseBtn.addEventListener('click', function () { setClosed(true); });
  if (toggle) toggle.addEventListener('click', function () { setClosed(false); });
  var stored = null;
  try { stored = localStorage.getItem('hrTocClosed'); } catch (e) {}
  if (stored === null) setClosed(window.matchMedia('(max-width: 1140px)').matches);
  else setClosed(stored === '1');

  // 只列「展開中」日期：日期為 lv1、當日主題/報告標題為 lv2
  function buildTOC() {
    tocList.innerHTML = '';
    var count = 0;
    days.forEach(function (d) {
      if (!d.open) return;
      var dayName = d.querySelector('summary').textContent.trim();
      addLink(d.querySelector('summary'), dayName, 'lv1', d);
      [].forEach.call(d.querySelectorAll('h2, h3'), function (h) {
        addLink(h, h.textContent.trim(), 'lv2', d);
      });
    });
    function addLink(target, name, cls, day) {
      if (!target.id) target.id = 'sec-' + (++uid);
      var li = document.createElement('li');
      var a = document.createElement('a');
      a.href = '#' + target.id;
      a.textContent = name;
      a.title = name;
      a.className = cls;
      a.addEventListener('click', function (e) {
        e.preventDefault();
        day.open = true;
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        if (window.matchMedia('(max-width: 1140px)').matches) setClosed(true);
      });
      li.appendChild(a);
      tocList.appendChild(li);
      count++;
    }
    if (count === 0) tocList.innerHTML = '<li class="empty">（展開任一日期以顯示大綱）</li>';
  }
  days.forEach(function (d) { d.addEventListener('toggle', buildTOC); });
  buildTOC();
})();
</script>
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
            lvl = min(len(m.group(1)) + 1, 4)   # md 的 # → h2、## → h3、### → h4(不進大綱)
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
