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
from pathlib import Path

from .leverage_ingest import CACHE, DATA, _day_rows, _fin, _token  # noqa: F401

logger = logging.getLogger(__name__)

MKT_FILES = ["mkt_margin", "mkt_short", "mkt_price", "mkt_daytrading", "mkt_buxian", "mkt_mktval"]


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


def ingest_margin_only(start: str, end: str):
    """只回補全市場融資融券（mkt_margin），供 52 週 rank 等長歷史指標用（1 call/天）。"""
    token = _token()
    DATA.mkdir(parents=True, exist_ok=True)
    rows = []
    for d in _trading_days(start, end):
        ds = d.isoformat()
        mg = _fin("TaiwanStockMarginPurchaseShortSale", ds, ds, token)
        for r in mg:
            rows.append({"id": r["stock_id"], "d": ds,
                         "mbal": r["MarginPurchaseTodayBalance"], "mbuy": r["MarginPurchaseBuy"],
                         "mrep": r["MarginPurchaseCashRepayment"], "mlim": r.get("MarginPurchaseLimit") or 0,
                         "sbal": r["ShortSaleTodayBalance"]})
        if mg:
            logger.info("融資 %s：%d 檔", ds, len(mg))
        time.sleep(0.3)
    n, tot = _save_flat("mkt_margin", rows, start, end)
    logger.info("mkt_margin +%d（庫存 %d）", n, tot)


def ingest_market(start: str, end: str):
    """抓 [start,end] 全市場槓桿資料，落地 mkt_*.json。"""
    token = _token()
    DATA.mkdir(parents=True, exist_ok=True)
    margin, short, price, daytr, buxian, mktval = [], [], [], [], [], []
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
        for r in _fin("TaiwanStockMarketValue", ds, ds, token):  # 全市場（不濾 U，供權重分母）
            if r.get("market_value"):
                mktval.append({"id": r["stock_id"], "d": ds, "mv": r["market_value"]})
        bx, _ = _day_rows(d)  # 沿用/建立 TWTA1U 快取（全市場）
        if bx:
            for code, nm, mgk, bxk in bx:
                if code in U:
                    buxian.append({"id": code, "d": ds, "bx": bxk, "mc": mgk})
                    names[code] = nm
        logger.info("全市場 %s：%d 檔", ds, len(U))
        time.sleep(0.4)

    for name, rows in (("mkt_margin", margin), ("mkt_short", short), ("mkt_price", price),
                       ("mkt_daytrading", daytr), ("mkt_buxian", buxian), ("mkt_mktval", mktval)):
        n, tot = _save_flat(name, rows, start, end)
        logger.info("%s +%d（庫存 %d）", name, n, tot)

    if names:  # 股名對照表（全市場表格用）
        p = DATA / "names.json"
        old = json.loads(p.read_text()) if p.exists() else {}
        old.update(names)
        p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")


# ---------- 個股期貨（OI / 基差 / 大戶偏空）----------
# 對照表 src/futures_mapping.json 由期交所 stockLists 產生（scripts/backfill_stock_futures.py
# 的 mapping()；新股票期貨掛牌時需 refresh 後 commit）。契約股數以表為準：
# 股票標準 2000/小型 100、ETF 標準 10000/小型 1000。
# mkt_fut: id,d, ol(標準口),om(小型口),os(股數等值張),bs(近月基差%可null),sk(大戶偏空可null),mo(大額市場OI)
# mkt_futoi.json: dashboard 快照 {date, rows:{id:[os,bs,sk]}}


def _load_fut_mapping():
    return json.loads((Path(__file__).resolve().parent / "futures_mapping.json").read_text())


def ingest_futures(start: str, end: str):
    """抓 [start,end] 個股期貨日資料＋大額交易人，彙總併入 mkt_fut、輸出 dashboard 快照。"""
    token = _token()
    mp = _load_fut_mapping()
    spot = {}
    prc_path = DATA / "mkt_price.json"
    if prc_path.exists():
        for r in json.loads(prc_path.read_text()):
            if r.get("c"):
                spot[(r["d"], r["id"])] = r["c"]

    rows = []
    for d in _trading_days(start, end):
        ds = d.isoformat()
        fut = _fin("TaiwanFuturesDaily", ds, ds, token)
        if not fut:
            continue
        acc = {}   # sid -> ol/om/shares/near_cd/near_close
        for r in fut:
            fid = r.get("futures_id") or ""
            if (r.get("trading_session") != "position" or len(fid) != 3
                    or "/" in str(r.get("contract_date", ""))):
                continue
            m = mp.get(fid[:2])
            if not m:
                continue
            sh = m["sh"]
            a = acc.setdefault(m["id"], {"ol": 0, "om": 0, "shares": 0,
                                         "near_cd": "999999", "near_close": None})
            oi = r.get("open_interest") or 0
            a["ol" if sh >= 2000 else "om"] += oi
            a["shares"] += oi * sh
            cd = str(r.get("contract_date", ""))
            if sh >= 2000 and (r.get("close") or 0) > 0 and cd < a["near_cd"]:
                a["near_cd"], a["near_close"] = cd, r["close"]

        trd = {}   # sid -> mo/wb/ws（標準+小型按市場OI加權）
        for r in _fin("TaiwanFuturesOpenInterestLargeTraders", ds, ds, token):
            m = mp.get(r.get("futures_id"))
            if not m or r.get("contract_type") != "all":
                continue
            t = trd.setdefault(m["id"], {"mo": 0, "wb": 0.0, "ws": 0.0})
            mo = max(r.get("market_open_interest") or 0, 1)
            t["mo"] += mo
            t["wb"] += (r.get("buy_top10_trader_open_interest_per") or 0) * mo
            t["ws"] += (r.get("sell_top10_trader_open_interest_per") or 0) * mo

        for sid, a in acc.items():
            sp = spot.get((ds, sid))
            bs = round((a["near_close"] / sp - 1) * 100, 2) if sp and a["near_close"] else None
            t = trd.get(sid)
            sk = round((t["ws"] - t["wb"]) / t["mo"], 1) if t else None
            rows.append({"id": sid, "d": ds, "ol": a["ol"], "om": a["om"],
                         "os": a["shares"] // 1000, "bs": bs, "sk": sk,
                         "mo": t["mo"] if t else 0})
        logger.info("個股期貨 %s：%d 檔", ds, len(acc))
        time.sleep(0.4)

    n, tot = _save_flat("mkt_fut", rows, start, end)
    logger.info("mkt_fut +%d（庫存 %d）", n, tot)

    hist = json.loads((DATA / "mkt_fut.json").read_text())
    if hist:  # dashboard 快照（最新一日）
        last = max(r["d"] for r in hist)
        snap = {r["id"]: [r["os"], r["bs"], r["sk"]] for r in hist if r["d"] == last}
        (DATA / "mkt_futoi.json").write_text(json.dumps(
            {"date": last, "source": "TAIFEX via FinMind", "rows": snap},
            ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        logger.info("mkt_futoi.json 快照 %s（%d 檔）", last, len(snap))
