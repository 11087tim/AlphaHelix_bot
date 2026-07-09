from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ReportsConfig:
    stocks: list[str]
    years: list[int]
    quarters: list[int]
    report_types: list[str]
    language: str
    workers: int
    min_interval_sec: float
    max_retries: int
    data_dir: Path
    cheap_model: str
    strong_model: str
    vision_model: str
    chunk_chars: int
    eval_sample_chunks: int
    analysis_topics: list[dict]
    vision_max_pages: int
    synthesis_enabled: bool


class ConfigError(RuntimeError):
    pass


def load_config(path: Path | None = None) -> ReportsConfig:
    path = path or PROJECT_ROOT / "reports_config.yaml"
    if not path.exists():
        raise ConfigError(f"找不到設定檔：{path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    stocks = [str(s).strip() for s in (raw.get("stocks") or []) if str(s).strip()]
    if not stocks:
        raise ConfigError("reports_config.yaml 的 stocks 是空的，至少要指定一檔股票。")

    valid_types = {"consolidated", "individual"}
    report_types = [t for t in (raw.get("report_types") or ["consolidated"]) if t in valid_types]
    if not report_types:
        raise ConfigError("report_types 必須至少包含 consolidated 或 individual。")

    language = str(raw.get("language", "zh")).lower()
    if language not in {"zh", "en"}:
        raise ConfigError("language 只能是 zh（中文）或 en（英文）。")

    data_dir = raw.get("data_dir", "reports_data")
    llm = raw.get("llm") or {}
    return ReportsConfig(
        stocks=stocks,
        years=[int(y) for y in (raw.get("years") or [])],
        quarters=[int(q) for q in (raw.get("quarters") or [1, 2, 3, 4])],
        report_types=report_types,
        language=language,
        workers=int(raw.get("workers", 5)),
        min_interval_sec=float(raw.get("min_interval_sec", 0.4)),
        max_retries=int(raw.get("max_retries", 3)),
        data_dir=PROJECT_ROOT / data_dir if not Path(data_dir).is_absolute() else Path(data_dir),
        cheap_model=llm.get("cheap_model", "anthropic/claude-haiku-4.5"),
        strong_model=llm.get("strong_model", "anthropic/claude-opus-4.8"),
        vision_model=llm.get("vision_model", "google/gemini-2.5-flash"),
        chunk_chars=int(llm.get("chunk_chars", 6000)),
        eval_sample_chunks=int(llm.get("eval_sample_chunks", 6)),
        analysis_topics=(raw.get("analysis") or {}).get("topics") or _DEFAULT_TOPICS,
        vision_max_pages=int((raw.get("analysis") or {}).get("vision_max_pages", 6)),
        synthesis_enabled=bool((raw.get("analysis") or {}).get("synthesis", True)),
    )


_DEFAULT_TOPICS = [
    {"name": "重大會計政策", "match": ["會計政策"], "mode": "text",
     "instruction": "整理重大會計政策要點，特別是影響收入/獲利認列、與同業不同、或今年有變動的部分。"},
    {"name": "營運部門/分部資訊", "match": ["營運部門", "部門資訊", "分部"], "mode": "vision",
     "instruction": "讀出各營運部門/分部的營收、獲利與資產數字，保留原始數字。"},
    {"name": "關係人交易", "match": ["關係人"], "mode": "vision",
     "instruction": "讀出主要關係人交易對象、交易類型（銷貨/進貨/資金貸與/背書保證等）與金額。"},
    {"name": "重大或有負債及承諾", "match": ["或有負債", "承諾"], "mode": "text",
     "instruction": "整理重大或有負債、未認列合約承諾、背書保證、重大資本支出承諾等重點與金額。"},
    {"name": "重大期後事項", "match": ["期後事項"], "mode": "text",
     "instruction": "整理資產負債表日之後發生的重大事項。"},
]
