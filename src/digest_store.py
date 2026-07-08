from __future__ import annotations

import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "digests.json"

# 保留最近幾份每小時摘要（約 3 天），避免檔案無限增長
MAX_DIGESTS = 72


class DigestStore:
    """儲存每小時產出的摘要。每筆 entry:
    {id, generated_at, account_sections, keyword_sections, emailed}"""

    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = path
        self.digests: list[dict] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.digests = data.get("digests", [])

    def append(self, entry: dict) -> None:
        self.digests.append(entry)
        self.digests = self.digests[-MAX_DIGESTS:]

    def unsent(self) -> list[dict]:
        """尚未寄信的摘要，依時間由舊到新。"""
        return [d for d in self.digests if not d.get("emailed")]

    def mark_emailed(self, ids: list[str]) -> None:
        id_set = set(ids)
        for d in self.digests:
            if d["id"] in id_set:
                d["emailed"] = True

    def recent(self, n: int) -> list[dict]:
        """最近 n 筆，依時間由新到舊（供網站顯示）。"""
        return list(reversed(self.digests[-n:]))

    def save(self) -> None:
        self.path.write_text(
            json.dumps({"digests": self.digests}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
