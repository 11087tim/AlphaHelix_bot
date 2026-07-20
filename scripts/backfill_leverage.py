#!/usr/bin/env python3
"""全量回補台股融資融券/借券/當沖（大盤+個股）到本地歷史庫（FinMind）。

實作在 src/leverage_ingest.py（單一真相，與每日增量共用）。
用法：python scripts/backfill_leverage.py [START] [END] [stock1 stock2 ...]
      預設 2026-01-20 ~ 2026-07-18，個股 2330 6182 2327 3167 3026
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.leverage_ingest import STOCKS, ingest_finmind  # noqa: E402

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    a = sys.argv[1:]
    start = a[0] if len(a) > 0 else "2026-01-20"
    end = a[1] if len(a) > 1 else "2026-07-18"
    stocks = a[2:] if len(a) > 2 else STOCKS
    print(f"回補 FinMind {start} ~ {end}｜個股 {', '.join(stocks)}")
    ingest_finmind(start, end, stocks)
    print("✅ 完成")
