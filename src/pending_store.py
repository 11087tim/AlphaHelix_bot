from __future__ import annotations

import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "pending.json"

# 安全上限，避免長時間未彙整時無限累積
MAX_PENDING = 1000


class PendingStore:
    """暫存「已抓取、尚未彙整」的原始推文。fetch 累積，synthesis 取出後清空。"""

    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = path
        self.tweets: list[dict] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.tweets = data.get("tweets", [])

    def add(self, tweets: list[dict]) -> None:
        self.tweets.extend(tweets)
        self.tweets = self.tweets[-MAX_PENDING:]

    def all(self) -> list[dict]:
        return self.tweets

    def clear(self) -> None:
        self.tweets = []

    def save(self) -> None:
        self.path.write_text(
            json.dumps({"tweets": self.tweets}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
