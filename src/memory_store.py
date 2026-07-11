"""跨時間記憶帳本（Phase 2）：把每份 digest 萃取成結構化立場紀錄，落地成 memory.json。
供合成時撈「本批提到之實體」的歷史立場軌跡，做趨勢/反轉/矛盾偵測。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "memory.json"
RETENTION_DAYS = 120   # 帳本保留天數
MAX_RECORDS = 4000     # 總筆數上限，避免無限增長

# 一筆紀錄：{date, generated_at, digest_id, entity, kind, stance(-2..2), claim, drivers}


class MemoryStore:
    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = path
        self.records: list[dict] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.records = data.get("records", [])

    def has_digest(self, digest_id: str) -> bool:
        """該 digest 是否已萃取過（供 backfill/重跑冪等）。"""
        return any(r.get("digest_id") == digest_id for r in self.records)

    def add_records(self, records: list[dict]) -> int:
        """加入一批紀錄；同 (digest_id, entity) 視為重複，以新的取代舊的。回傳實際新增/更新數。"""
        if not records:
            return 0
        incoming_keys = {(r.get("digest_id"), r.get("entity")) for r in records}
        self.records = [
            r for r in self.records
            if (r.get("digest_id"), r.get("entity")) not in incoming_keys
        ]
        self.records.extend(records)
        self._prune()
        return len(records)

    def _prune(self) -> None:
        cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
        self.records = [r for r in self.records if str(r.get("date", "")) >= cutoff]
        self.records.sort(key=lambda r: str(r.get("date", "")))
        if len(self.records) > MAX_RECORDS:
            self.records = self.records[-MAX_RECORDS:]

    def for_entities(self, entities: set[str], days: int = RETENTION_DAYS) -> list[dict]:
        """撈出指定實體、近 days 天的紀錄，依日期由舊到新。"""
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        out = [
            r for r in self.records
            if r.get("entity") in entities and str(r.get("date", "")) >= cutoff
        ]
        out.sort(key=lambda r: str(r.get("date", "")))
        return out

    def save(self) -> None:
        self.path.write_text(
            json.dumps({"records": self.records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
