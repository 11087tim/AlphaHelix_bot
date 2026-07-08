from __future__ import annotations

import json
from pathlib import Path

DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "state.json"

# 避免 state.json 無限增長，只保留最近看過的 N 個 id
MAX_SEEN_IDS = 5000


class Storage:
    def __init__(self, path: Path = DEFAULT_STATE_PATH):
        self.path = path
        self._seen_ids: set[str] = set()
        self._user_ids: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._seen_ids = set(data.get("seen_ids", []))
            self._user_ids = data.get("user_ids", {})

    def filter_new(self, tweets: list[dict]) -> list[dict]:
        return [t for t in tweets if t["id"] not in self._seen_ids]

    def mark_seen(self, tweets: list[dict]) -> None:
        for t in tweets:
            self._seen_ids.add(t["id"])

    def get_user_id(self, username: str) -> str | None:
        """從快取取帳號 ID（避免每次都向 X 查一次 User: Read）。"""
        return self._user_ids.get(username)

    def set_user_id(self, username: str, user_id: str) -> None:
        self._user_ids[username] = user_id

    def save(self) -> None:
        ids = list(self._seen_ids)[-MAX_SEEN_IDS:]
        self.path.write_text(
            json.dumps(
                {"seen_ids": ids, "user_ids": self._user_ids},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
