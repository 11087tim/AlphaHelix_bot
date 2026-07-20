#!/usr/bin/env python3
"""全量回補全市場台股槓桿資料庫（mkt_*.json）。實作在 src/leverage_market.py。

用法：python scripts/backfill_market.py [START] [END]   預設 2026-01-20 ~ 2026-07-18
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.leverage_market import ingest_market  # noqa: E402

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    a = sys.argv[1:]
    start = a[0] if len(a) > 0 else "2026-01-20"
    end = a[1] if len(a) > 1 else "2026-07-18"
    print(f"回補全市場 {start} ~ {end}")
    ingest_market(start, end)
    print("✅ 完成")
