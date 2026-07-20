#!/usr/bin/env python3
"""回補台股槓桿/融資融券歷史資料（大盤 + 指定個股）到本地歷史庫。

資料源：FinMind（token 取自 how_wealt_earnings/.env 的 FINMIND_TOKEN）
輸出：data/leverage/*.json（每個 dataset 一檔，個股表含 stock_id 欄位）
用法：python scripts/backfill_leverage.py [START] [END] [stock1 stock2 ...]
      預設 2026-06-17 ~ 2026-07-18，個股 2330 6182 2327 3167 3026
"""
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

API = "https://api.finmindtrade.com/api/v4/data"
OUT = Path(__file__).resolve().parent.parent / "data" / "leverage"

# 大盤層級（免 data_id）
MARKET_DATASETS = {
    "market_margin": "TaiwanStockTotalMarginPurchaseShortSale",   # 大盤融資融券（金額/張數）
    "market_maintenance": "TaiwanTotalExchangeMarginMaintenance",  # 大盤融資維持率
}
# 個股層級（需 data_id）
STOCK_DATASETS = {
    "stock_margin": "TaiwanStockMarginPurchaseShortSale",   # 個股融資融券（張數+限額）
    "stock_shortbal": "TaiwanDailyShortSaleBalances",       # 個股空方：融券 vs 借券賣出
    "stock_lending": "TaiwanStockSecuritiesLending",        # 個股借券成交明細
    "stock_daytrading": "TaiwanStockDayTrading",            # 個股當沖
}
NAMES = {"2330": "台積電", "6182": "合晶", "2327": "國巨", "3167": "大量", "3026": "禾伸堂"}


def _token() -> str:
    for line in open("/Users/chenyanting/how_wealt_earnings/.env"):
        if line.strip().startswith("FINMIND_TOKEN"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("找不到 FINMIND_TOKEN")


def fetch(dataset, start, end, token, data_id=None):
    params = {"dataset": dataset, "start_date": start, "end_date": end, "token": token}
    if data_id:
        params["data_id"] = data_id
    for attempt in range(4):
        r = requests.get(API, params=params, timeout=45)
        if r.status_code == 200:
            return r.json().get("data", [])
        if r.status_code in (402, 429):  # 額度：等一下重試
            time.sleep(8 * (attempt + 1))
            continue
        r.raise_for_status()
    return []


def main() -> None:
    args = sys.argv[1:]
    start = args[0] if len(args) > 0 else "2026-06-17"
    end = args[1] if len(args) > 1 else "2026-07-18"
    stocks = args[2:] if len(args) > 2 else ["2330", "6182", "2327", "3167", "3026"]
    token = _token()
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"回補區間 {start} ~ {end}｜個股 {', '.join(stocks)}\n")

    # 大盤
    for key, ds in MARKET_DATASETS.items():
        rows = fetch(ds, start, end, token)
        (OUT / f"{key}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        days = sorted({r["date"] for r in rows})
        print(f"[大盤] {key:20s} {len(rows):4d} 筆  {days[0] if days else '—'} ~ {days[-1] if days else '—'}")

    # 個股（每 dataset 匯總所有股票成一檔）
    for key, ds in STOCK_DATASETS.items():
        allrows = []
        per = defaultdict(int)
        for sid in stocks:
            rows = fetch(ds, start, end, token, data_id=sid)
            for r in rows:
                r.setdefault("stock_id", sid)
            allrows.extend(rows)
            per[sid] = len(rows)
            time.sleep(0.3)  # 禮貌節流
        (OUT / f"{key}.json").write_text(json.dumps(allrows, ensure_ascii=False, indent=1), encoding="utf-8")
        detail = " ".join(f"{s}:{per[s]}" for s in stocks)
        print(f"[個股] {key:20s} {len(allrows):4d} 筆  ({detail})")

    print(f"\n✅ 已存到 {OUT}")


if __name__ == "__main__":
    main()
