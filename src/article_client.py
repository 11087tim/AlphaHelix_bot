"""文章來源（RSS → 全文 → LLM 蒸餾成要點 items），供餵進 digest 合成。

獨立於 podcast 管線的文字文章模組：適用 LessWrong 精選這類「思想/前沿論述」來源，
蒸餾不限投資訊號——核心論點、關鍵洞見、對 AI/科技/市場的判斷都抽。
items 形狀同推文（id/author/text/created_at/url/source/media），source 標 "article:<名稱>"。
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

SEEN_PATH = Path(__file__).resolve().parent.parent / "articles_seen.json"
MAX_ARTICLE_CHARS = 100000  # 蒸餾輸入上限（LW 長文足夠）

_SYSTEM = (
    "你是研究助理。以下是一篇長文（可能是 AI/科技/理性思維/市場相關的論述文章）。"
    "抽出文章中真正有價值的內容：核心論點、關鍵洞見、重要的預測或判斷、與 AI 發展/科技產業/市場可能相關的推論。"
    "不限投資訊號——思想性、前瞻性的觀點也要抽，但每點必須是文章實際說的，不可延伸捏造。"
    "【忽略】鋪陳、舉例細節、致謝、離題段落。用繁體中文，每點一句話、具體。"
    "輸出 JSON 陣列，每筆 {\"point\": 一句話重點}；寧缺勿濫、不要硬湊，最多 10 點。只輸出 JSON 陣列。"
)


def _text(el, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _strip_html(html: str) -> str:
    """粗略去標籤：block 標籤換行、其餘移除、實體解碼、收斂空白。"""
    s = re.sub(r"<(br|/p|/div|/li|/h[1-6]|/blockquote)[^>]*>", "\n", html, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def fetch_new_articles(sources: list[dict], window_hours: float, max_articles: int,
                       seen) -> list[dict]:
    """抓各來源時間窗內、未讀過的文章。sources=[{name, url}]。
    回傳 [{id, name, title, url, published, text}]。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    out: list[dict] = []
    for src in sources:
        name, feed_url = src["name"], src["url"]
        try:
            resp = requests.get(feed_url, timeout=30,
                                headers={"User-Agent": "Mozilla/5.0 (AlphaHelix digest)"})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("抓取文章 feed 失敗（%s）：%s", name, exc)
            continue
        picked = 0
        for item in root.iter("item"):
            if picked >= max_articles:
                break
            link = _text(item, "link")
            guid = _text(item, "guid") or link
            aid = f"article:{guid}"
            if not link or seen.is_seen(aid):
                continue
            try:
                published = parsedate_to_datetime(_text(item, "pubDate"))
            except Exception:  # noqa: BLE001
                published = None
            if published and published < cutoff:
                continue
            # LW 等站的 RSS 在 description 內含全文 HTML；沒有就退回只用標題
            body = _text(item, "description")
            content_el = item.find("{http://purl.org/rss/1.0/modules/content/}encoded")
            if content_el is not None and content_el.text:
                body = content_el.text
            text = _strip_html(body)[:MAX_ARTICLE_CHARS]
            out.append({"id": aid, "name": name, "title": _text(item, "title"),
                        "url": link, "published": published, "text": text})
            picked += 1
    return out


def distill(article: dict, model: str, api_key: str) -> list[dict]:
    """把一篇文章蒸餾成要點 items（形狀同推文）。"""
    body = (article.get("text") or "").strip()
    if len(body) < 200:  # 太短代表 feed 沒給全文，只留標題沒意義
        logger.info("文章「%s」無全文可蒸餾，略過。", article.get("title", "")[:30])
        return []
    name, title = article["name"], article.get("title", "")
    user = f"來源：{name}\n標題：{title}\n\n全文：\n{body}"
    try:
        from . import summarizer
        from .memory_extract import _parse_json_array
        data = summarizer._post_chat(api_key, {
            "model": model,
            "messages": [{"role": "system", "content": _SYSTEM},
                         {"role": "user", "content": user}],
        })
        points = _parse_json_array(data["choices"][0]["message"]["content"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("文章蒸餾失敗，略過：%s", exc)
        return []

    published = article.get("published")
    created_at = published.strftime("%Y-%m-%d %H:%M") if published else ""
    items: list[dict] = []
    for i, p in enumerate(points, 1):
        point = str(p.get("point", "")).strip() if isinstance(p, dict) else ""
        if not point:
            continue
        items.append({
            "id": f"{article['id']}#{i}",
            "author": f"📄{name}",
            "text": f"（{title}）{point}",
            "created_at": created_at,
            "url": article["url"],
            "source": f"article:{name}",
            "media": [],
        })
    logger.info("蒸餾文章「%s」→ %d 條要點", title[:30], len(items))
    return items
