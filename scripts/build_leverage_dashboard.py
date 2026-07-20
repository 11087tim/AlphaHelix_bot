#!/usr/bin/env python3
"""產生台股去槓桿壓力儀表板 → docs/leverage.html（實作在 src/leverage_dashboard.py）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.leverage_dashboard import build  # noqa: E402

if __name__ == "__main__":
    print(f"✅ 產生 {build()}")
