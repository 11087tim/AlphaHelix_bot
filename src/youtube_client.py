"""YouTube 長影音來源：頻道 RSS 找新片 → 抓免費字幕（youtube-transcript-api）。

比 podcast 更省——不用下載音檔/轉錄，直接取 YouTube 自動字幕。之後與 podcast 共用蒸餾與 🎙️ 分類。
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

_NS = {"a": "http://www.w3.org/2005/Atom",
       "yt": "http://www.youtube.com/xml/schemas/2015",
       "media": "http://search.yahoo.com/mrss/"}
_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
_TRANSCRIPT_LANGS = ["en", "zh-Hant", "zh-Hans", "zh", "ja"]


def resolve_channel_id(handle: str) -> str | None:
    """由 @handle 或頻道網址解析 channel_id（UC…）。放進 config 前用來查。"""
    handle = handle.strip()
    url = handle if handle.startswith("http") else f"https://www.youtube.com/@{handle.lstrip('@')}"
    try:
        html = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).text
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析頻道失敗 %s：%s", handle, exc)
        return None
    m = re.search(r'"externalId":"(UC[\w-]+)"', html) or \
        re.search(r'youtube\.com/channel/(UC[\w-]+)', html)
    return m.group(1) if m else None


def _parse_feed(xml_bytes: bytes) -> tuple[str, list[dict]]:
    root = ET.fromstring(xml_bytes)
    show = root.findtext("a:title", namespaces=_NS) or ""
    videos = []
    for e in root.findall("a:entry", _NS):
        vid = e.findtext("yt:videoId", namespaces=_NS)
        if not vid:
            continue
        pub_raw = e.findtext("a:published", namespaces=_NS) or ""
        try:
            published = datetime.fromisoformat(pub_raw) if pub_raw else None
        except ValueError:
            published = None
        videos.append({
            "id": f"yt:{vid}",
            "video_id": vid,
            "show": show,
            "title": e.findtext("a:title", namespaces=_NS) or "",
            "published": published,
            "page_url": f"https://www.youtube.com/watch?v={vid}",
        })
    return show, videos


def _fetch_feed(cid: str) -> tuple[str, list[dict]]:
    resp = requests.get(_FEED.format(cid=cid), timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return _parse_feed(resp.content)


def fetch_new_videos(channels: list[str], window_hours: float, per_channel: int, seen) -> list[dict]:
    """每個頻道各取近 window_hours 內、未處理過的最新 per_channel 支影片。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    _old = datetime.min.replace(tzinfo=timezone.utc)
    found: list[dict] = []
    for cid in channels:
        try:
            show, videos = _fetch_feed(cid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("讀取 YouTube 頻道失敗 %s：%s", cid, exc)
            continue
        fresh = [v for v in videos
                 if not seen.is_seen(v["id"]) and not (v["published"] and v["published"] < cutoff)]
        fresh.sort(key=lambda v: v["published"] or _old, reverse=True)
        picked = fresh[:per_channel]
        found.extend(picked)
        if picked:
            logger.info("頻道「%s」：取最新未處理 %d 支", show, len(picked))
    found.sort(key=lambda v: v["published"] or _old, reverse=True)
    return found


def get_transcript(video_id: str) -> str:
    """抓 YouTube 免費字幕，回傳純文字。無字幕會丟出例外由呼叫端處理。"""
    from youtube_transcript_api import YouTubeTranscriptApi
    fetched = YouTubeTranscriptApi().fetch(video_id, languages=_TRANSCRIPT_LANGS)
    return " ".join(s.text for s in fetched if s.text).strip()


def mark_all_seen(channels: list[str], seen) -> int:
    n = 0
    for cid in channels:
        try:
            _show, videos = _fetch_feed(cid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("讀取 YouTube 頻道失敗 %s：%s", cid, exc)
            continue
        for v in videos:
            if not seen.is_seen(v["id"]):
                seen.mark(v["id"])
                n += 1
    return n
