"""台股槓桿資料抓取＋落地（FinMind 融資融券/借券/當沖 + TWSE 不限用途借款）。

canonical 抓取邏輯，供三處共用：
- scripts/backfill_leverage.py、scripts/backfill_buxian.py（全量回補 CLI）
- src.main 的 `leverage` mode（每日增量）
合併策略採「視窗覆蓋」：只重寫 [start,end] 區間的列、保留區間外全部歷史，
因此每天回抓近幾日即可補漏又不丟舊資料（idempotent）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass

logger = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent.parent / "data" / "leverage"
FINMIND_API = "https://api.finmindtrade.com/api/v4/data"
TWSE_TWTA1U = "https://www.twse.com.tw/rwd/zh/marginTrading/TWTA1U"

STOCKS = ["2330", "6182", "2327", "3167", "3026"]
NAMES = {"2330": "台積電", "6182": "合晶", "2327": "國巨", "3167": "大量", "3026": "禾伸堂"}

MARKET_DATASETS = {
    "market_margin": "TaiwanStockTotalMarginPurchaseShortSale",
    "market_maintenance": "TaiwanTotalExchangeMarginMaintenance",
}
STOCK_DATASETS = {
    "stock_margin": "TaiwanStockMarginPurchaseShortSale",
    "stock_shortbal": "TaiwanDailyShortSaleBalances",
    "stock_lending": "TaiwanStockSecuritiesLending",
    "stock_daytrading": "TaiwanStockDayTrading",
}
# 不限用途款項借貸「今日餘額」欄位（groups: col15-21 證券商不限用途款項借貸）
COL_BUXIAN_TODAY, COL_MARGIN_TODAY = 20, 6


def _token() -> str:
    """FinMind token：優先環境變數 FINMIND_TOKEN（VM），退回本機 how_wealt_earnings/.env（Mac 開發）。"""
    t = os.getenv("FINMIND_TOKEN")
    if t:
        return t
    dev = Path.home() / "how_wealt_earnings" / ".env"
    if dev.exists():
        for line in dev.read_text().splitlines():
            if line.strip().startswith("FINMIND_TOKEN"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("找不到 FINMIND_TOKEN（請設環境變數或 .env）")


def _fin(dataset, start, end, token, data_id=None):
    params = {"dataset": dataset, "start_date": start, "end_date": end, "token": token}
    if data_id:
        params["data_id"] = data_id
    for attempt in range(4):
        r = requests.get(FINMIND_API, params=params, timeout=45)
        if r.status_code == 200:
            return r.json().get("data", [])
        if r.status_code in (402, 429):
            time.sleep(8 * (attempt + 1))
            continue
        r.raise_for_status()
    return []


def _num(x):
    x = (x or "").replace(",", "").strip()
    try:
        return int(x)
    except ValueError:
        return 0


def _fetch_buxian_day(d: date):
    r = requests.get(TWSE_TWTA1U, params={"date": d.strftime("%Y%m%d"), "response": "json"}, timeout=30)
    j = r.json()
    if j.get("stat") != "OK" or not j.get("data"):
        return None
    return j["data"]


def _save_window(name, new_rows, start, end, scope_ids=None):
    """視窗覆蓋合併：丟掉舊檔中落在 [start,end]（且在 scope_ids 內）的列，換成 new_rows，其餘保留。"""
    path = DATA / f"{name}.json"
    old = json.loads(path.read_text()) if path.exists() else []

    def in_window(r):
        if not (start <= r["date"] <= end):
            return False
        if scope_ids is not None and r.get("stock_id") not in scope_ids:
            return False
        return True

    merged = [r for r in old if not in_window(r)] + new_rows
    merged.sort(key=lambda r: (r["date"], str(r.get("stock_id", ""))))
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(new_rows), len(merged)


def ingest(start: str, end: str, stocks=None):
    """抓 [start,end] 的大盤+個股+不限用途，視窗覆蓋落地到 data/leverage/。"""
    ingest_finmind(start, end, stocks)
    ingest_buxian(start, end, stocks)


def ingest_finmind(start: str, end: str, stocks=None):
    """FinMind：大盤 + 個股 融資融券/借券/當沖。"""
    stocks = stocks or STOCKS
    DATA.mkdir(parents=True, exist_ok=True)
    token = _token()

    for key, ds in MARKET_DATASETS.items():
        rows = _fin(ds, start, end, token)
        n, tot = _save_window(key, rows, start, end)
        logger.info("[大盤] %s +%d（庫存 %d）", key, n, tot)

    for key, ds in STOCK_DATASETS.items():
        allrows = []
        for sid in stocks:
            rows = _fin(ds, start, end, token, data_id=sid)
            for r in rows:
                r.setdefault("stock_id", sid)
            allrows.extend(rows)
            time.sleep(0.3)
        n, tot = _save_window(key, allrows, start, end, scope_ids=set(stocks))
        logger.info("[個股] %s +%d（庫存 %d）", key, n, tot)


def ingest_buxian(start: str, end: str, stocks=None):
    stocks = stocks or STOCKS
    DATA.mkdir(parents=True, exist_ok=True)
    tset = set(stocks)
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    stock_rows, market_rows = [], []
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            try:
                data = _fetch_buxian_day(d)
            except Exception as exc:  # noqa: BLE001
                logger.warning("TWSE %s 讀取失敗：%s", d, exc)
                data = None
            if data:
                tot_bx = tot_mg = 0
                for row in data:
                    bx = _num(row[COL_BUXIAN_TODAY])
                    tot_bx += bx
                    tot_mg += _num(row[COL_MARGIN_TODAY])
                    if row[0] in tset:
                        stock_rows.append({
                            "date": d.isoformat(), "stock_id": row[0], "name": row[1],
                            "buxian_balance_kshares": bx,
                            "margin_collateral_kshares": _num(row[COL_MARGIN_TODAY]),
                        })
                market_rows.append({
                    "date": d.isoformat(), "buxian_total_kshares": tot_bx,
                    "margin_collateral_total_kshares": tot_mg, "n_stocks": len(data),
                })
            time.sleep(0.6)
        d += timedelta(days=1)
    n1, t1 = _save_window("buxian_market", market_rows, start, end)
    n2, t2 = _save_window("buxian_stock", stock_rows, start, end, scope_ids=tset)
    logger.info("[不限用途] market +%d（%d）, stock +%d（%d）", n1, t1, n2, t2)
