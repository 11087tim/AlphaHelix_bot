#!/usr/bin/env python3
"""全量回補 TWSE「證券商不限用途款項借貸」擔保品餘額（TWTA1U）到本地歷史庫。

實作在 src/leverage_ingest.py（單一真相，與每日增量共用）。
用法：python scripts/backfill_buxian.py [START] [END]
      預設 2026-01-20 ~ 2026-07-18
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.leverage_ingest import ingest_buxian  # noqa: E402

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    a = sys.argv[1:]
    start = a[0] if len(a) > 0 else "2026-01-20"
    end = a[1] if len(a) > 1 else "2026-07-18"
    print(f"回補 TWSE 不限用途 {start} ~ {end}")
    ingest_buxian(start, end)
    print("✅ 完成")
