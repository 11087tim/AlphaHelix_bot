"""全市場個股「推估融資維持率」回補（2010-01-04 → 今，含下市股）。

資料源（FinMind）：
- TaiwanStockMarginPurchaseShortSale：逐「交易日」全市場查詢（不給 data_id），
  一天一請求；有信用交易的股票天然全包含（上市+上櫃+已下市）。
- TaiwanStockPrice：逐「個股」一次抓全期（universe = margin 資料出現過的所有 stock_id）。
- 交易日曆用 TAIEX 收盤序列。

維持率推估法（無官方個股數據，此為公開資料下的標準近似）：
- 推估融資平均成本：融資餘額增加時，以「當日成交均價（成交金額/成交量）」計入新倉成本；
  餘額減少時平均成本不變；餘額歸零後下次建倉重新起算。
- 期初（2010 首見）餘額成本以首日均價起算（偏誤隨換手遞減）。
- 維持率 = 收盤價 / (推估平均成本 × 融資成數) × 100，成數：上市 0.6、上櫃 0.5。
已知限制：除權息未調整、處置股成數調整未考慮、非券商「整戶」維持率。

落地：
  data/leverage/maintenance/raw_margin/{year}.parquet   （逐年，重跑該年全量覆蓋，冪等）
  data/leverage/maintenance/raw_price/{stock_id}.parquet
  data/leverage/maintenance/maintenance.parquet          （date, stock_id, close, balance, est_cost, maint_ratio）

用法：python3 scripts/backfill_maintenance.py [--phase all|margin|price|compute]
可中斷重跑：margin 以「年」為單位跳過已完成年份、price 以檔案存在跳過。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.leverage_ingest import _token  # noqa: E402

API = "https://api.finmindtrade.com/api/v4/data"
OUT = ROOT / "data" / "leverage" / "maintenance"
RAW_MARGIN = OUT / "raw_margin"
RAW_PRICE = OUT / "raw_price"
START = "2010-01-01"
TODAY = date.today().isoformat()


def _set_paths(out_dir: Path, start: str) -> None:
    global OUT, RAW_MARGIN, RAW_PRICE, START
    OUT = out_dir
    RAW_MARGIN = OUT / "raw_margin"
    RAW_PRICE = OUT / "raw_price"
    START = start

SESSION = requests.Session()


def fin(token: str, **params):
    params = {"token": token, **params}
    for attempt in range(5):
        try:
            r = SESSION.get(API, params=params, timeout=120)
        except requests.RequestException as e:  # noqa: PERF203
            print(f"  ! network {e}; retry {attempt+1}", flush=True)
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 200:
            j = r.json()
            if j.get("msg") == "success" or "data" in j:
                return j.get("data", [])
        if r.status_code in (402, 429):
            wait = 60 * (attempt + 1)
            print(f"  ! rate limited ({r.status_code}); sleep {wait}s", flush=True)
            time.sleep(wait)
            continue
        print(f"  ! http {r.status_code}: {r.text[:200]}; retry {attempt+1}", flush=True)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"FinMind 連續失敗: {params.get('dataset')} {params}")


def trading_days(token: str) -> list[str]:
    rows = fin(token, dataset="TaiwanStockPrice", data_id="TAIEX",
               start_date=START, end_date=TODAY)
    days = [r["date"] for r in rows]
    print(f"交易日 {len(days)} 天（{days[0]} → {days[-1]}）", flush=True)
    return days


# ---------- Phase A: margin（逐日全市場，按年落地） ----------

def phase_margin(token: str) -> None:
    RAW_MARGIN.mkdir(parents=True, exist_ok=True)
    days = trading_days(token)
    by_year: dict[str, list[str]] = {}
    for d in days:
        by_year.setdefault(d[:4], []).append(d)
    cur_year = TODAY[:4]
    for year, ydays in sorted(by_year.items()):
        fp = RAW_MARGIN / f"{year}.parquet"
        if fp.exists() and year != cur_year:
            print(f"[margin] {year} 已存在，跳過", flush=True)
            continue
        rows: list[dict] = []
        t0 = time.time()
        for i, d in enumerate(ydays):
            rows.extend(fin(token, dataset="TaiwanStockMarginPurchaseShortSale",
                            start_date=d, end_date=d))
            time.sleep(0.05)
            if (i + 1) % 50 == 0:
                print(f"[margin] {year}: {i+1}/{len(ydays)} 天, 累計 {len(rows)} 列, "
                      f"{time.time()-t0:.0f}s", flush=True)
        df = pd.DataFrame(rows)
        df.to_parquet(fp, index=False)
        print(f"[margin] {year} 完成: {len(ydays)} 天 {len(df)} 列 → {fp.name}", flush=True)


# ---------- Phase B: price（逐檔全期） ----------

def margin_universe() -> list[str]:
    ids: set[str] = set()
    for fp in sorted(RAW_MARGIN.glob("*.parquet")):
        ids |= set(pd.read_parquet(fp, columns=["stock_id"])["stock_id"].unique())
    return sorted(ids)


def phase_price(token: str) -> None:
    RAW_PRICE.mkdir(parents=True, exist_ok=True)
    universe = margin_universe()
    print(f"[price] universe {len(universe)} 檔", flush=True)
    t0 = time.time()
    for i, sid in enumerate(universe):
        fp = RAW_PRICE / f"{sid}.parquet"
        if fp.exists():
            continue
        rows = fin(token, dataset="TaiwanStockPrice", data_id=sid,
                   start_date=START, end_date=TODAY)
        pd.DataFrame(rows).to_parquet(fp, index=False)
        time.sleep(0.05)
        if (i + 1) % 100 == 0:
            print(f"[price] {i+1}/{len(universe)} 檔, {time.time()-t0:.0f}s", flush=True)
    print(f"[price] 完成 {len(universe)} 檔", flush=True)


# ---------- Phase C: compute 維持率 ----------

def stock_types(token: str) -> dict[str, str]:
    rows = fin(token, dataset="TaiwanStockInfo")
    return {r["stock_id"]: r.get("type", "twse") for r in rows}


def compute() -> None:
    token = _token()
    types = stock_types(token)
    margin = pd.concat(
        [pd.read_parquet(fp, columns=["date", "stock_id",
                                      "MarginPurchaseTodayBalance",
                                      "MarginPurchaseYesterdayBalance"])
         for fp in sorted(RAW_MARGIN.glob("*.parquet"))],
        ignore_index=True)
    print(f"[compute] margin {len(margin)} 列, {margin['stock_id'].nunique()} 檔", flush=True)

    out_frames: list[pd.DataFrame] = []
    for n, (sid, g) in enumerate(margin.groupby("stock_id", sort=True)):
        pf = RAW_PRICE / f"{sid}.parquet"
        if not pf.exists():
            continue
        px = pd.read_parquet(pf)
        if px.empty or "close" not in px.columns:  # 無成交資料的股票存成零欄位檔
            continue
        px = px[["date", "close", "Trading_money", "Trading_Volume"]]
        px = px[px["close"] > 0]  # close=0 為無成交日（停牌/全額交割），不可入成本與維持率
        g = g.sort_values("date").merge(px, on="date", how="inner")
        if g.empty:
            continue
        vwap = (g["Trading_money"] / g["Trading_Volume"].replace(0, pd.NA)).fillna(g["close"])
        # 融資成數：現制上市/上櫃皆 6 成（上櫃 2015 年由 5 成調高；經 TEJ 交叉驗證
        # 用 0.5 會造成 OTC 系統性 +44pt 偏差、0.6 後降到 +11pt）。
        # TODO 全量回補 2010-2015 前的上櫃段要用 0.5，切換日期待查證後參數化。
        ratio_pct = 0.6
        tb = g["MarginPurchaseTodayBalance"].to_numpy(dtype="float64")
        yb = g["MarginPurchaseYesterdayBalance"].to_numpy(dtype="float64")
        vw = vwap.to_numpy(dtype="float64")
        close = g["close"].to_numpy(dtype="float64")
        cost = float("nan")
        costs = [float("nan")] * len(g)
        for i in range(len(g)):
            if tb[i] <= 0:
                cost = float("nan")
                continue
            if cost != cost:            # NaN → 期初/重新建倉
                cost = vw[i]
            elif tb[i] > yb[i] and tb[i] > 0:
                add = tb[i] - yb[i]
                base = min(yb[i], tb[i])
                cost = (cost * base + vw[i] * add) / (base + add)
            costs[i] = cost
        g_out = pd.DataFrame({
            "date": g["date"], "stock_id": sid, "close": close,
            "margin_balance": tb.astype("int64"), "est_cost": costs,
        })
        g_out["maint_ratio"] = g_out["close"] / (g_out["est_cost"] * ratio_pct) * 100
        out_frames.append(g_out)
        if (n + 1) % 300 == 0:
            print(f"[compute] {n+1} 檔...", flush=True)

    result = pd.concat(out_frames, ignore_index=True)
    OUT.mkdir(parents=True, exist_ok=True)
    result.to_parquet(OUT / "maintenance.parquet", index=False)
    print(f"[compute] 完成: {len(result)} 列, {result['stock_id'].nunique()} 檔 "
          f"→ maintenance.parquet", flush=True)
    latest = result[result["date"] == result["date"].max()]
    print(latest.nsmallest(10, "maint_ratio")[["date", "stock_id", "close", "maint_ratio"]]
          .to_string(index=False), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="all", choices=["all", "margin", "price", "compute"])
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--out-dir", default=str(OUT))
    args = ap.parse_args()
    _set_paths(Path(args.out_dir), args.start)
    token = _token()
    if args.phase in ("all", "margin"):
        phase_margin(token)
    if args.phase in ("all", "price"):
        phase_price(token)
    if args.phase in ("all", "compute"):
        compute()


if __name__ == "__main__":
    main()
