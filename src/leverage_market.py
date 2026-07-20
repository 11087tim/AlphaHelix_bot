"""全市場台股槓桿資料庫（按日抓、一次全市場，供日後個股 FLR 強平風險螢幕用）。

抓法：FinMind 不帶 data_id → 單一日期回傳全市場（融資/空方/股價/當沖各 1 call/天）；
不限用途沿用 TWTA1U 每日快取（已含全部股票）。以「融資可交易股票」為宇宙 U（~2200 檔），
其餘資料集都濾到 U。欄位精簡、短鍵、compact JSON，落地 data/leverage/mkt_*.json。

各檔 schema（皆 list[dict]，數量單位「張」；金額「元」；股價「元」；成交量「股」）：
- mkt_margin:   id,d, mbal(融資餘額),mbuy(融資買進),mrep(融資現償),mlim(融資限額),sbal(融券餘額)
- mkt_short:    id,d, fin(融券餘額),sbl(借券賣出餘額)          # 皆已由股換算為張
- mkt_price:    id,d, c(收盤),v(成交量股)
- mkt_daytrading:id,d, dv(當沖量股),db(當沖買元),dsl(當沖賣元)
- mkt_buxian:   id,d, bx(不限用途仟股),mc(融資擔保仟股)
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta

from .leverage_ingest import CACHE, DATA, _day_rows, _fin, _token  # noqa: F401

logger = logging.getLogger(__name__)

MKT_FILES = ["mkt_margin", "mkt_short", "mkt_price", "mkt_daytrading", "mkt_buxian"]


def _save_flat(name, rows, start, end):
    """視窗覆蓋合併（全市場、依日期）；compact JSON 省空間。"""
    path = DATA / f"{name}.json"
    old = json.loads(path.read_text()) if path.exists() else []
    kept = [r for r in old if not (start <= r["d"] <= end)]
    merged = kept + rows
    merged.sort(key=lambda r: (r["d"], r["id"]))
    path.write_text(json.dumps(merged, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(rows), len(merged)


def _trading_days(start, end):
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def ingest_market(start: str, end: str):
    """抓 [start,end] 全市場槓桿資料，落地 mkt_*.json。"""
    token = _token()
    DATA.mkdir(parents=True, exist_ok=True)
    margin, short, price, daytr, buxian = [], [], [], [], []
    names = {}

    for d in _trading_days(start, end):
        ds = d.isoformat()
        mg = _fin("TaiwanStockMarginPurchaseShortSale", ds, ds, token)
        if not mg:  # 非交易日
            continue
        U = {r["stock_id"] for r in mg}
        for r in mg:
            margin.append({"id": r["stock_id"], "d": ds,
                           "mbal": r["MarginPurchaseTodayBalance"], "mbuy": r["MarginPurchaseBuy"],
                           "mrep": r["MarginPurchaseCashRepayment"], "mlim": r.get("MarginPurchaseLimit") or 0,
                           "sbal": r["ShortSaleTodayBalance"]})
        for r in _fin("TaiwanDailyShortSaleBalances", ds, ds, token):
            if r["stock_id"] in U:
                short.append({"id": r["stock_id"], "d": ds,
                              "fin": r["MarginShortSalesCurrentDayBalance"] // 1000,
                              "sbl": r["SBLShortSalesCurrentDayBalance"] // 1000})
        for r in _fin("TaiwanStockPrice", ds, ds, token):
            if r["stock_id"] in U:
                price.append({"id": r["stock_id"], "d": ds, "c": r.get("close"), "v": r.get("Trading_Volume")})
        for r in _fin("TaiwanStockDayTrading", ds, ds, token):
            if r["stock_id"] in U:
                daytr.append({"id": r["stock_id"], "d": ds, "dv": r.get("Volume"),
                              "db": r.get("BuyAmount"), "dsl": r.get("SellAmount")})
        bx, _ = _day_rows(d)  # 沿用/建立 TWTA1U 快取（全市場）
        if bx:
            for code, nm, mgk, bxk in bx:
                if code in U:
                    buxian.append({"id": code, "d": ds, "bx": bxk, "mc": mgk})
                    names[code] = nm
        logger.info("全市場 %s：%d 檔", ds, len(U))
        time.sleep(0.4)

    for name, rows in (("mkt_margin", margin), ("mkt_short", short), ("mkt_price", price),
                       ("mkt_daytrading", daytr), ("mkt_buxian", buxian)):
        n, tot = _save_flat(name, rows, start, end)
        logger.info("%s +%d（庫存 %d）", name, n, tot)

    if names:  # 股名對照表（全市場表格用）
        p = DATA / "names.json"
        old = json.loads(p.read_text()) if p.exists() else {}
        old.update(names)
        p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
