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


MAX_TOTAL_PER_RUN = 25  # 安全上限：單次跨所有 feed 最多處理幾集，避免成本失控


def _fetch_feed(url: str):
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return _parse_feed(resp.content)


def fetch_new_episodes(feeds: list[str], window_hours: float, per_feed: int,
                       seen: SeenStore) -> list[dict]:
    """每個 feed 各取近 window_hours 內、未處理過的最新 per_feed 集；全部彙整（新到舊、總量有上限）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    _old = datetime.min.replace(tzinfo=timezone.utc)
    found: list[dict] = []
    for url in feeds:
        try:
            show, episodes = _fetch_feed(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("讀取 podcast feed 失敗 %s：%s", url, exc)
            continue
        fresh = [e for e in episodes
                 if not seen.is_seen(e["id"]) and not (e["published"] and e["published"] < cutoff)]
        fresh.sort(key=lambda e: e["published"] or _old, reverse=True)
        picked = fresh[:per_feed]
        found.extend(picked)
        if picked:
            logger.info("feed「%s」：%d 集，取最新未處理 %d 集", show, len(episodes), len(picked))
    found.sort(key=lambda e: e["published"] or _old, reverse=True)
    return found[:MAX_TOTAL_PER_RUN]


def mark_all_seen(feeds: list[str], seen: SeenStore) -> int:
    """把所有 feed 目前的集數全標為已讀（基準線），之後只處理新發布的集。回傳標記數。"""
    n = 0
    for url in feeds:
        try:
            _show, episodes = _fetch_feed(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("讀取 podcast feed 失敗 %s：%s", url, exc)
            continue
        for ep in episodes:
            if not seen.is_seen(ep["id"]):
                seen.mark(ep["id"])
                n += 1
    return n
