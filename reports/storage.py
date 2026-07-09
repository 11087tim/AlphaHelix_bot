from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path


class ReportStorage:
    """儲存下載的財報 PDF/文字，並用 manifest 追蹤狀態（去重、可續跑）。
    manifest 以 (股號,年,季) 為鍵，執行緒安全。"""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir)
        self.raw_dir = self.root / "raw"
        self.text_dir = self.root / "text"
        self.manifest_path = self.root / "manifest.json"
        self._lock = threading.Lock()
        self.manifest: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    @staticmethod
    def key(co_id: str, year: int, quarter: int, language: str) -> str:
        return f"{co_id}_{year}Q{quarter}_{language}"

    def is_done(self, co_id: str, year: int, quarter: int, language: str) -> bool:
        entry = self.manifest.get(self.key(co_id, year, quarter, language))
        if not entry or entry.get("status") != "done":
            return False
        # 檔案仍在才算完成（避免 manifest 說完成但檔案被刪）
        return bool(entry.get("pdf_path") and (self.root / entry["pdf_path"]).exists())

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    def save_pdf(self, co_id: str, year: int, quarter: int, report_type: str,
                 language: str, filename: str, content: bytes) -> Path:
        out = self.raw_dir / co_id / f"{year}Q{quarter}_{report_type}_{language}.pdf"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(content)
        with self._lock:
            self.manifest[self.key(co_id, year, quarter, language)] = {
                "status": "done",
                "co_id": co_id, "year": year, "quarter": quarter,
                "report_type": report_type, "language": language,
                "source_filename": filename,
                "pdf_path": self._rel(out),
                "bytes": len(content),
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }
        return out

    def mark_failed(self, co_id: str, year: int, quarter: int, language: str, error: str) -> None:
        with self._lock:
            self.manifest[self.key(co_id, year, quarter, language)] = {
                "status": "failed",
                "co_id": co_id, "year": year, "quarter": quarter, "language": language,
                "error": error,
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
            }

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.manifest_path.write_text(
                json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def done_pdfs(self) -> list[dict]:
        return [e for e in self.manifest.values() if e.get("status") == "done" and e.get("pdf_path")]
