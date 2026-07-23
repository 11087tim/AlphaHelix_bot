"""用 headless Chromium 抓 valuelist 熱榜（宝塔防火牆 JS 驗證需真瀏覽器）。"""
from __future__ import annotations

import logging

from . import config

logger = logging.getLogger(__name__)

_EXTRACT_JS = """
() => {
  const sections = [...document.querySelectorAll('.hot-reports')];
  const result = [];
  sections.forEach(sec => {
    let h = sec.previousElementSibling;
    while (h && !/今日|本周|本月/.test(h.textContent)) h = h.previousElementSibling;
    const secName = h ? h.textContent.trim().slice(0, 10) : 'unknown';
    [...sec.querySelectorAll('a')]
      .filter(a => /【\\d+页】|-\\d{8}/.test(a.textContent))
      .forEach(a => {
        const li = a.closest('li') || a.parentElement;
        result.push({
          section: secName,
          title: a.textContent.trim(),
          url: a.href,
          meta: li ? li.textContent.replace(a.textContent, '').trim().replace(/\\s+/g, ' ') : '',
        });
      });
  });
  return result;
}
"""

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def scrape_hot_titles() -> list[dict]:
    """回傳 [{section, title, url, meta}, ...]；宝塔驗證會自動跳轉，等文章列表出現即可。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=UA)
            page.goto(config.VALUELIST_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector(".hot-reports a", timeout=45_000)
            rows = page.evaluate(_EXTRACT_JS)
        finally:
            browser.close()
    logger.info("valuelist 抓到 %d 筆（含跨區重複）", len(rows))
    return rows
