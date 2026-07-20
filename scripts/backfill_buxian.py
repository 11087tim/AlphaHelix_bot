#!/usr/bin/env python3
"""回補 TWSE「證券商不限用途款項借貸」擔保品餘額（TWTA1U）。

散戶拿股票質押借錢（不限用途）的擔保品張數——融資完全看不到的另一條散戶槓桿。
資料源：TWSE 借貸款項擔保品餘額表 TWTA1U（每日、公開、單位仟股）。
欄位分組（groups）：col2-7=融資, col8-14=證券業務借貸, col15-21=證券商不限用途款項借貸,
                    col22-28=證金擔保放款, col29-35=證金交割融資。
→ 我們取 col20 = 不限用途「今日餘額」(仟股)。
輸出：data/leverage/buxian_stock.json（5檔）、buxian_market.json（全市場加總）。
用法：python scripts/backfill_buxian.py [START] [END]
"""
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

URL = "https://www.twse.com.tw/rwd/zh/marginTrading/TWTA1U"
OUT = Path(__file__).resolve().parent.parent / "data" / "leverage"
TARGETS = {"2330", "6182", "2327", "3167", "3026"}
COL_BUXIAN_TODAY = 20   # 證券商不限用途款項借貸 今日餘額（仟股）
COL_MARGIN_TODAY = 6    # 融資擔保品 今日餘額（仟股，僅供參考）


def num(x):
    x = (x or "").replace(",", "").strip()
    try:
        return int(x)
    except ValueError:
        return 0


def fetch_day(d):
    ds = d.strftime("%Y%m%d")
    r = requests.get(URL, params={"date": ds, "response": "json"}, timeout=30)
    j = r.json()
    if j.get("stat") != "OK" or not j.get("data"):
        return None
    return j["data"]


def main():
    args = sys.argv[1:]
    start = date.fromisoformat(args[0]) if len(args) > 0 else date(2026, 6, 17)
    end = date.fromisoformat(args[1]) if len(args) > 1 else date(2026, 7, 18)
    OUT.mkdir(parents=True, exist_ok=True)

    stock_rows, market_rows = [], []
    d = start
    while d <= end:
        if d.weekday() < 5:  # 只試工作日
            try:
                data = fetch_day(d)
            except Exception as e:
                print(f"  {d} 讀取失敗：{e}")
                data = None
            if data:
                total_bx = total_mg = 0
                for row in data:
                    bx = num(row[COL_BUXIAN_TODAY])
                    total_bx += bx
                    total_mg += num(row[COL_MARGIN_TODAY])
                    if row[0] in TARGETS:
                        stock_rows.append({
                            "date": d.isoformat(), "stock_id": row[0], "name": row[1],
                            "buxian_balance_kshares": bx,          # 不限用途今日餘額(仟股)
                            "margin_collateral_kshares": num(row[COL_MARGIN_TODAY]),
                        })
                market_rows.append({
                    "date": d.isoformat(),
                    "buxian_total_kshares": total_bx,      # 全市場不限用途擔保品(仟股)
                    "margin_collateral_total_kshares": total_mg,
                    "n_stocks": len(data),
                })
                print(f"  {d} ✓ 不限用途總擔保品 {total_bx:,} 仟股 / {len(data)} 檔")
            time.sleep(0.6)  # 對 TWSE 禮貌節流
        d += timedelta(days=1)

    (OUT / "buxian_stock.json").write_text(json.dumps(stock_rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "buxian_market.json").write_text(json.dumps(market_rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n✅ 個股 {len(stock_rows)} 筆、市場 {len(market_rows)} 天 → {OUT}")


if __name__ == "__main__":
    main()
