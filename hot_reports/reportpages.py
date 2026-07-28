"""為每篇已摘要的報告生成獨立分頁（內嵌完整 PDF + 下載 + LLM 摘要），
並生成一個卡片式總覽索引 docs/reports.html。

PDF 複製進 docs/reports/ 才能被 GitHub Pages 公開；只保留最近 REPORTS_KEEP_DAYS 天，
避免 repo 無限膨脹（注意：git 歷史仍會保留刪除前的檔案）。
"""
from __future__ import annotations

import html
import logging
import shutil
from datetime import datetime, timedelta

from . import config
from .site import _md_block   # 複用摘要 markdown → HTML

logger = logging.getLogger(__name__)

_HEAD = """<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<style>
  :root { --bg:#f7f9fb; --card:#fff; --fg:#1c2733; --muted:#5b6b7b; --line:#e3e9f0; --accent:#2f6fab; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0f1620; --card:#16202b; --fg:#dbe6f1; --muted:#8fa2b5; --line:#274156; --accent:#7cc0f0; }
  }
  * { box-sizing:border-box; }
  body { margin:0; padding:24px 16px 60px; background:var(--bg); color:var(--fg);
         font:17px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",sans-serif; }
  .wrap { max-width:960px; margin:0 auto; }
  a { color:var(--accent); }
  .navlinks a { display:inline-block; padding:6px 14px; border-radius:999px; font-size:.9rem;
    color:var(--accent); background:var(--card); border:1px solid var(--line); text-decoration:none; margin:0 8px 8px 0; }
  h1 { font-size:1.6rem; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:.9rem; margin:0 0 20px; }
  .meta { color:var(--muted); font-size:.92rem; margin:2px 0 16px; }
  .btnrow { margin:16px 0; }
  .btn { display:inline-block; padding:9px 18px; border-radius:10px; text-decoration:none; font-weight:600;
    background:var(--accent); color:#fff; margin-right:10px; }
  .btn.ghost { background:transparent; color:var(--accent); border:1px solid var(--line); }
  embed, iframe.pdf { width:100%; height:82vh; border:1px solid var(--line); border-radius:10px; background:#fff; }
  h2 { font-size:1.3rem; color:var(--accent); margin:26px 0 8px; }
  ul { padding-left:22px; } li { margin:2px 0; }
  hr { border:none; border-top:1px solid var(--line); margin:18px 0; }
  .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:14px; }
  .rcard { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; display:flex; flex-direction:column; }
  .rcard .inst { font-weight:700; color:var(--accent); font-size:.9rem; }
  .rcard .ttl { margin:6px 0 10px; font-size:1.02rem; line-height:1.4; flex:1 1 auto; }
  .rcard .rmeta { color:var(--muted); font-size:.82rem; margin-bottom:12px; }
  .rcard .acts a { font-size:.9rem; margin-right:12px; text-decoration:none; }
  .foot { color:var(--muted); font-size:.8rem; margin-top:30px; }
</style>"""


def _within_keep(r: dict) -> bool:
    cutoff = (datetime.now() - timedelta(days=config.REPORTS_KEEP_DAYS)).strftime("%Y-%m-%d")
    ref = (r.get("nash_date") or r.get("first_seen") or "")[:10]
    return ref >= cutoff


def _report_html(r: dict) -> str:
    inst = html.escape(r.get("nash_securities") or r.get("institution") or "")
    title = html.escape(r.get("nash_title") or r.get("title_en") or "")
    meta = f"{r.get('nash_date') or ''}｜{r.get('nash_pages') or '?'} 頁"
    pdf = f"{r['nash_id']}.pdf"
    summary_html = _md_block(r.get("summary") or "")
    return f"""<!DOCTYPE html><html lang="zh-Hant"><head>{_HEAD}<title>{inst}｜{title}</title></head>
<body><div class="wrap">
  <p class="navlinks"><a href="../reports.html">← 全部報告</a><a href="../hot_reports.html">📑 每日彙整</a></p>
  <h1>📄 {title}</h1>
  <p class="meta"><b>{inst}</b>　{html.escape(meta)}</p>
  <div class="btnrow">
    <a class="btn" href="{pdf}" download>⬇ 下載 PDF</a>
    <a class="btn ghost" href="{pdf}" target="_blank" rel="noopener">↗ 新分頁開啟</a>
  </div>
  <embed src="{pdf}" type="application/pdf">
  <h2>🤖 LLM 摘要</h2>
  {summary_html}
  <p class="foot">PDF 為原始研報；摘要由 LLM 自動生成，僅供研究參考，非投資建議。</p>
</div></body></html>"""


def _index_html(cards: list[dict]) -> str:
    blocks = []
    for c in cards:
        blocks.append(
            f'<div class="rcard"><div class="inst">{html.escape(c["inst"])}</div>'
            f'<div class="ttl">{html.escape(c["title"])}</div>'
            f'<div class="rmeta">{html.escape(c["meta"])}</div>'
            f'<div class="acts"><a href="reports/{c["id"]}.html">📄 看報告</a>'
            f'<a href="reports/{c["id"]}.pdf" download>⬇ 下載</a></div></div>')
    grid = "\n".join(blocks) or '<p class="sub">目前尚無報告。</p>'
    return f"""<!DOCTYPE html><html lang="zh-Hant"><head>{_HEAD}<title>研報庫</title></head>
<body><div class="wrap">
  <h1>🗂 研報庫</h1>
  <p class="sub">已收集的外資研報（近 {config.REPORTS_KEEP_DAYS} 天，共 {len(cards)} 篇）。點卡片看內嵌 PDF 或直接下載。</p>
  <p class="navlinks"><a href="index.html">🏠 每日 X 摘要</a><a href="hot_reports.html">📑 每日研報彙整</a><a href="leverage.html">📊 台股槓桿儀表板</a></p>
  <div class="cards">
{grid}
  </div>
  <p class="foot">PDF 為原始研報，僅供研究參考。</p>
</div></body></html>"""


def render_report_pages(reports: dict) -> int:
    """為 summarized 且有 PDF、且在保留天數內的報告生成 PDF+分頁+索引。
    回傳生成的報告數。"""
    config.REPORTS_WEB_DIR.mkdir(parents=True, exist_ok=True)

    targets = [r for r in reports.values()
               if r.get("status") == "summarized" and r.get("pdf") and r.get("nash_id")
               and _within_keep(r)]
    targets.sort(key=lambda r: (r.get("nash_date") or "", r.get("views") or 0), reverse=True)

    keep_ids = set()
    cards = []
    for r in targets:
        rid = r["nash_id"]
        keep_ids.add(str(rid))
        src = config.PDF_DIR / r["pdf"]
        if not src.exists():
            logger.warning("PDF 不存在，跳過分頁：%s", r["pdf"])
            continue
        dst = config.REPORTS_WEB_DIR / f"{rid}.pdf"
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
        (config.REPORTS_WEB_DIR / f"{rid}.html").write_text(_report_html(r), encoding="utf-8")
        cards.append({
            "id": rid,
            "inst": r.get("nash_securities") or r.get("institution") or "",
            "title": r.get("nash_title") or r.get("title_en") or "",
            "meta": f"{r.get('nash_date') or ''}｜{r.get('nash_pages') or '?'} 頁"
                    + (f"｜{r['views']} 次" if r.get("views") else ""),
        })

    # 清掉超過保留天數的舊 PDF/分頁（git 歷史仍保留）
    for f in config.REPORTS_WEB_DIR.iterdir():
        if f.suffix in (".pdf", ".html") and f.stem not in keep_ids:
            f.unlink()

    (config.DOCS_DIR / "reports.html").write_text(_index_html(cards), encoding="utf-8")
    logger.info("研報庫已生成：%d 篇分頁 + 索引", len(cards))
    return len(cards)
