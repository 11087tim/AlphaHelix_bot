"""Podcast 長訪談來源：讀 RSS → 找出近期、尚未處理過的新集（含音檔直連）。

RSS 是開放標準（Apple/Spotify 背後都讀它），從 feed 就能拿到每集的 mp3 網址，平台無關、無爬蟲。
"""
from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

SEEN_PATH = Path(__file__).resolve().parent.parent / "podcast_seen.json"
_ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


class SeenStore:
    """記錄已處理過的集數 id，避免重複轉錄。"""

    def __init__(self, path: Path = SEEN_PATH):
        self.path = path
        self.seen: set[str] = set()
        if path.exists():
            self.seen = set(json.loads(path.read_text(encoding="utf-8")).get("seen", []))

    def is_seen(self, ep_id: str) -> bool:
        return ep_id in self.seen

    def mark(self, ep_id: str) -> None:
        self.seen.add(ep_id)

    def save(self) -> None:
        # 有界，避免無限增長
        ids = list(self.seen)[-2000:]
        self.path.write_text(json.dumps({"seen": ids}, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def _text(el, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _parse_feed(xml_bytes: bytes) -> tuple[str, list[dict]]:
    """回傳 (節目名稱, 集數清單)。"""
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    show = _text(channel, "title") if channel is not None else ""
    episodes = []
    for it in root.findall(".//item"):
        enc = it.find("enclosure")
        audio = enc.get("url") if enc is not None else ""
        if not audio:
            continue
        guid = _text(it, "guid") or audio
        pub_raw = _text(it, "pubDate")
        try:
            published = parsedate_to_datetime(pub_raw) if pub_raw else None
            if published and published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            published = None
        episodes.append({
            "id": guid,
            "show": show,
            "title": _text(it, "title"),
            "published": published,
            "audio_url": audio,
            "page_url": _text(it, "link"),
            "duration": _text(it, f"{_ITUNES}duration"),
        })
    return show, episodes


def fetch_new_episodes(feeds: list[str], window_hours: float, max_episodes: int,
                       seen: SeenStore) -> list[dict]:
    """跨所有 feed 找出近 window_hours 內、未處理過的新集，最多 max_episodes 集（新到舊）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    found: list[dict] = []
    for url in feeds:
        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            show, episodes = _parse_feed(resp.content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("讀取 podcast feed 失敗 %s：%s", url, exc)
            continue
        for ep in episodes:
            if seen.is_seen(ep["id"]):
                continue
            if ep["published"] and ep["published"] < cutoff:
                continue
            found.append(ep)
        logger.info("feed「%s」：%d 集，新且在時間窗內 %d 集",
                    show, len(episodes), sum(1 for e in found if e["show"] == show))
    found.sort(key=lambda e: e["published"] or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    return found[:max_episodes]
