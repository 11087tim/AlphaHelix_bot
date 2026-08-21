"""深查報告庫：落地每題深查結果（裁定/一句話發現/全文），供 /deepdive/ 頁面渲染與去重。
一筆紀錄：{date, generated_at, digest_id, topic, entities, verdict, takeaway, report}
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "deepdives.json"
RETENTION_DAYS = 120


class DeepdiveStore:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = path
        self.records: list[dict] = []
        if self.path.exists():
            self.records = json.loads(self.path.read_text(encoding="utf-8")).get("records", [])

    def has_digest(self, digest_id: str) -> bool:
        """該 digest 的候選題是否已深查過（重跑冪等）。"""
        return any(r.get("digest_id") == digest_id for r in self.records)

    def add_records(self, records: list[dict]) -> None:
        self.records.extend(records)
        cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
        self.records = [r for r in self.records if str(r.get("date", "")) >= cutoff]
        self.records.sort(key=lambda r: (str(r.get("date", "")), str(r.get("generated_at", ""))))

    def days(self) -> list[dict]:
        """依日期由新到舊分組：[{date, topics:[record, ...]}, ...]（供 /deepdive/ 頁渲染）。
        鍵名用 topics 不用 items——Jinja 屬性查找會撞到 dict.items 內建方法。"""
        by_date: dict[str, list[dict]] = {}
        for r in self.records:
            by_date.setdefault(str(r.get("date", "")), []).append(r)
        return [{"date": d, "topics": by_date[d]} for d in sorted(by_date, reverse=True)]

    def save(self) -> None:
        self.path.write_text(
            json.dumps({"records": self.records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
