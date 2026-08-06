from __future__ import annotations

import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from .config import PROJECT_ROOT

logger = logging.getLogger(__name__)

API_URL = "https://api.finmindtrade.com/api/v4/data"
# token 主要放在 how_wealt_earnings 專案的 .env（歷史因素），其次才是本專案 .env
_TOKEN_ENVS = [PROJECT_ROOT / ".env", Path.home() / "how_wealt_earnings" / ".env"]


def get_token() -> str:
    for p in _TOKEN_ENVS:
        if not os.environ.get("FINMIND_TOKEN"):
            load_dotenv(p)
    token = os.environ.get("FINMIND_TOKEN", "")
    if not token:
        raise RuntimeError("缺少 FINMIND_TOKEN（找過：" + "、".join(str(p) for p in _TOKEN_ENVS) + "）")
    return token


def _fetch(dataset: str, stock: str, start_date: str, token: str) -> list[dict]:
    params = {"dataset": dataset, "token": token}
    if stock:
        params["data_id"] = stock
    if start_date:
        params["start_date"] = start_date
    resp = requests.get(API_URL, params=params, timeout=60)
    data = resp.json()
    if data.get("msg") != "success":
        raise RuntimeError(f"FinMind {dataset} 失敗：{data.get('msg')}")
    return data.get("data", [])


def latest_shares(stock: str, token: str) -> dict | None:
    """最新一季股本（TaiwanStockBalanceSheet 的 CapitalStock，元/面額10）→ 股數。
    回傳 {date, capital_ntd, shares}；查無回 None。
    注意：期末股本非 EPS 用的加權平均股數，極少數非 10 元面額個股會失真。"""
    rows = [r for r in _fetch("TaiwanStockBalanceSheet", stock, "2024-01-01", token)
            if r["type"] == "CapitalStock"]
    if not rows:
        return None
    last = max(rows, key=lambda r: r["date"])
    return {"date": last["date"], "capital_ntd": last["value"],
            "shares": int(last["value"] / 10)}


_EXCLUDE_CATEGORIES = {"ETF", "ETN", "Index", "大盤", "受益證券", "存託憑證", "所有證券"}


def all_market_tickers(token: str) -> list[dict]:
    """全市場上市(twse)/上櫃(tpex)普通股清單（TaiwanStockInfo）。
    只留 4 碼純數字、非 0 開頭（排除 ETF）、非 91xx（排除 TDR）、非指數/受益證券類。
    回傳 [{stock_id, stock_name, market}]，依代號排序。"""
    rows = _fetch("TaiwanStockInfo", "", "", token)
    seen: dict[str, dict] = {}
    for r in rows:
        sid = str(r.get("stock_id", "")).strip()
        if (r.get("type") not in ("twse", "tpex") or len(sid) != 4 or not sid.isdigit()
                or sid.startswith("0") or sid.startswith("91")
                or r.get("industry_category") in _EXCLUDE_CATEGORIES):
            continue
        seen.setdefault(sid, {"stock_id": sid, "stock_name": r.get("stock_name", ""),
                              "market": r["type"]})
    return [seen[k] for k in sorted(seen)]


_PL_TYPES = ["Revenue", "GrossProfit", "OperatingExpenses", "OperatingIncome",
             "TotalNonoperatingIncomeAndExpense", "PreTaxIncome", "IncomeAfterTaxes", "EPS"]


def quarterly_income(stock: str, token: str, start_date: str = "2023-01-01") -> list[dict]:
    """季損益表（FinMind TaiwanStockFinancialStatements，單季值、元）。
    回傳依季排序的 [{date, Revenue, GrossProfit, OperatingExpenses, ...}]，缺項為 None。"""
    rows = _fetch("TaiwanStockFinancialStatements", stock, start_date, token)
    by_date: dict[str, dict] = {}
    for r in rows:
        if r["type"] in _PL_TYPES:
            by_date.setdefault(r["date"], {"date": r["date"]})[r["type"]] = r["value"]
    return [by_date[d] for d in sorted(by_date)]


def balance_sheet_series(stock: str, token: str, start_date: str = "2023-06-01") -> list[dict]:
    """資產負債表關鍵科目逐季（元，時點值）。缺項為 None。"""
    wanted = {"CashAndCashEquivalents": "現金及約當現金", "AccountsReceivableNet": "應收帳款淨額",
              "Inventories": "存貨", "CurrentContractLiabilities": "合約負債-流動",
              "TotalAssets": "資產總額", "Equity": "權益總額"}
    rows = _fetch("TaiwanStockBalanceSheet", stock, start_date, token)
    by_date: dict[str, dict] = {}
    for r in rows:
        if r["type"] in wanted:
            by_date.setdefault(r["date"], {"date": r["date"]})[r["type"]] = r["value"]
    return [by_date[d] for d in sorted(by_date)]


_CF_TYPES = {"NetCashInflowFromOperatingActivities": "營運現金流",
             "CashProvidedByInvestingActivities": "投資現金流",
             "CashFlowsProvidedFromFinancingActivities": "籌資現金流",
             "PropertyAndPlantAndEquipment": "取得不動產廠房設備",
             "Depreciation": "折舊"}


def cash_flow_series(stock: str, token: str, start_date: str = "2023-06-01") -> list[dict]:
    """現金流量表關鍵科目逐季（元）。FinMind 給的是年內累計，這裡差分還原單季：
    Q1 照用；Qn ＝ 累計n − 累計n-1（跨年重置）。"""
    rows = _fetch("TaiwanStockCashFlowsStatement", stock, start_date, token)
    by_date: dict[str, dict] = {}
    for r in rows:
        if r["type"] in _CF_TYPES:
            by_date.setdefault(r["date"], {"date": r["date"]})[r["type"]] = r["value"]
    dates = sorted(by_date)
    out = []
    for i, d in enumerate(dates):
        rec = {"date": d}
        prev = by_date[dates[i - 1]] if i and dates[i - 1][:4] == d[:4] else {}
        for t in _CF_TYPES:
            cur = by_date[d].get(t)
            rec[t] = cur - prev.get(t, 0) if cur is not None else None
        out.append(rec)
    return out


def month_revenue(stock: str, token: str, start_date: str = "2024-01-01") -> list[dict]:
    """月營收（元），依月份排序。用於：(1)推導單季營收 (2)最新月份即時驗證轉換是否發生。"""
    rows = _fetch("TaiwanStockMonthRevenue", stock, start_date, token)
    out = [{"year": r["revenue_year"], "month": r["revenue_month"], "revenue": r["revenue"]}
           for r in rows]
    return sorted(out, key=lambda r: (r["year"], r["month"]))
