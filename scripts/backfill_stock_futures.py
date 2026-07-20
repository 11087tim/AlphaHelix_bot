"""個股期貨三件套回補（2010 → 今）：OI 總量 + 大額交易人買賣方 + 基差。

資料源：
- FinMind TaiwanFuturesDaily：逐日全市場（不帶 data_id），含各契約月/日夜盤。
- FinMind TaiwanFuturesOpenInterestLargeTraders：逐日全市場，個股期貨用兩碼代號（CD）。
- FinMind TaiwanStockPrice：標的現貨收盤（算基差），逐檔全期。
- 期交所 stockLists 對照表：期貨代碼 ↔ 標的證券代號 ↔ 契約股數（快取 mapping.csv）。

註：股票期貨 2010-01-25 上市，2010 起抓即完整歷史。大額交易人「特定法人」
是個股層級最接近法人方向的欄位（三大法人期貨資料只有股票期貨合計，拆不到個股）。

落地 data/leverage/futures/：
  raw_futures/{year}.parquet    raw_traders/{year}.parquet    raw_spot/{sid}.parquet
  mapping.csv
  stock_futures_oi.parquet  （date, stock_id, oi_std_lots, oi_mini_lots, oi_shares,
                             near_close, spot_close, basis_pct, mkt_oi,
                             buy_top10_pct, sell_top10_pct,
                             buy_top10_specific, sell_top10_specific, top10_skew）

用法：python3 scripts/backfill_stock_futures.py [--phase all|futures|traders|spot|build]
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
OUT = ROOT / "data" / "leverage" / "futures"
RAW_FUT = OUT / "raw_futures"
RAW_TRD = OUT / "raw_traders"
RAW_SPOT = OUT / "raw_spot"
START = "2010-01-04"
TODAY = date.today().isoformat()
TAIFEX_MAP_URL = "https://www.taifex.com.tw/cht/2/stockLists"


def _set_paths(out_dir: Path, start: str) -> None:
    global OUT, RAW_FUT, RAW_TRD, RAW_SPOT, START
    OUT = out_dir
    RAW_FUT = OUT / "raw_futures"
    RAW_TRD = OUT / "raw_traders"
    RAW_SPOT = OUT / "raw_spot"
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
    raise RuntimeError(f"FinMind 連續失敗: {params.get('dataset')}")


def trading_days(token: str) -> list[str]:
    rows = fin(token, dataset="TaiwanStockPrice", data_id="TAIEX",
               start_date=START, end_date=TODAY)
    days = [r["date"] for r in rows]
    print(f"交易日 {len(days)} 天（{days[0]} → {days[-1]}）", flush=True)
    return days


def _daily_fullmarket(token: str, dataset: str, out_dir: Path, tag: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    days = trading_days(token)
    by_year: dict[str, list[str]] = {}
    for d in days:
        by_year.setdefault(d[:4], []).append(d)
    cur_year = TODAY[:4]
    for year, ydays in sorted(by_year.items()):
        fp = out_dir / f"{year}.parquet"
        if fp.exists() and year != cur_year:
            print(f"[{tag}] {year} 已存在，跳過", flush=True)
            continue
        rows: list[dict] = []
        t0 = time.time()
        for i, d in enumerate(ydays):
            rows.extend(fin(token, dataset=dataset, start_date=d, end_date=d))
            time.sleep(0.05)
            if (i + 1) % 50 == 0:
                print(f"[{tag}] {year}: {i+1}/{len(ydays)} 天, 累計 {len(rows)} 列, "
                      f"{time.time()-t0:.0f}s", flush=True)
        pd.DataFrame(rows).to_parquet(fp, index=False)
        print(f"[{tag}] {year} 完成: {len(ydays)} 天 {len(rows)} 列", flush=True)


def phase_futures(token: str) -> None:
    _daily_fullmarket(token, "TaiwanFuturesDaily", RAW_FUT, "futures")


def phase_traders(token: str) -> None:
    _daily_fullmarket(token, "TaiwanFuturesOpenInterestLargeTraders", RAW_TRD, "traders")


# ---------- 對照表 ----------

def mapping(refresh: bool = False) -> pd.DataFrame:
    """期貨商品代碼 → (stock_id, name, 契約股數)。快取 mapping.csv。"""
    fp = OUT / "mapping.csv"
    if fp.exists() and not refresh:
        return pd.read_csv(fp, dtype=str).assign(shares=lambda d: d["shares"].astype(int))
    r = requests.get(TAIFEX_MAP_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    r.encoding = "utf-8"
    tables = [t for t in pd.read_html(r.text) if t.shape[0] > 50]
    t = tables[0]
    cols = [str(c).replace(" ", "") for c in t.columns]
    t.columns = cols
    shares_col = next(c for c in cols if "股數" in c or "受益權" in c)
    m = pd.DataFrame({
        "prefix": t.iloc[:, 0].astype(str).str.strip(),
        "stock_id": t.iloc[:, 2].astype(str).str.strip(),
        "name": t.iloc[:, 3].astype(str).str.strip(),
        "shares": pd.to_numeric(t[shares_col], errors="coerce").fillna(2000).astype(int),
    })
    m = m[m["prefix"].str.len() == 2]
    OUT.mkdir(parents=True, exist_ok=True)
    m.to_csv(fp, index=False)
    print(f"[mapping] {len(m)} 檔標的 → mapping.csv", flush=True)
    return m


# ---------- 現貨收盤 ----------

def phase_spot(token: str) -> None:
    RAW_SPOT.mkdir(parents=True, exist_ok=True)
    ids = sorted(mapping()["stock_id"].unique())
    print(f"[spot] {len(ids)} 檔標的", flush=True)
    for i, sid in enumerate(ids):
        fp = RAW_SPOT / f"{sid}.parquet"
        if fp.exists():
            continue
        rows = fin(token, dataset="TaiwanStockPrice", data_id=sid,
                   start_date=START, end_date=TODAY)
        pd.DataFrame(rows).to_parquet(fp, index=False)
        time.sleep(0.05)
        if (i + 1) % 50 == 0:
            print(f"[spot] {i+1}/{len(ids)}", flush=True)
    print("[spot] 完成", flush=True)


# ---------- build ----------

def build() -> None:
    mp = mapping()
    pref_shares = dict(zip(mp["prefix"], mp["shares"]))
    pref_sid = dict(zip(mp["prefix"], mp["stock_id"]))

    fut = pd.concat([pd.read_parquet(f) for f in sorted(RAW_FUT.glob("*.parquet"))],
                    ignore_index=True)
    fut = fut[(fut["trading_session"] == "position")
              & (~fut["contract_date"].astype(str).str.contains("/"))]
    fut["prefix"] = fut["futures_id"].str[:2]
    fut = fut[fut["futures_id"].str.len() == 3]
    fut = fut[fut["prefix"].isin(pref_sid)]
    fut["stock_id"] = fut["prefix"].map(pref_sid)
    fut["shares"] = fut["prefix"].map(pref_shares)
    fut["is_mini"] = fut["shares"] < 2000  # 小型契約：對照表股數 100 股（標準 2000）
    print(f"[build] 個股期貨列: {len(fut)}, 標的: {fut['stock_id'].nunique()}", flush=True)

    # OI 彙總（口數與股數等值）
    g = fut.groupby(["date", "stock_id"])
    oi = g.apply(lambda x: pd.Series({
        "oi_std_lots": x.loc[~x.is_mini, "open_interest"].sum(),
        "oi_mini_lots": x.loc[x.is_mini, "open_interest"].sum(),
        "oi_shares": (x["open_interest"] * x["shares"]).sum(),
    })).reset_index()

    # 近月：各(date,stock_id) 取最近契約月的標準契約 → 基差用收盤、OI 另出一欄
    std = fut[~fut.is_mini & (fut["close"] > 0)]
    near = (std.sort_values("contract_date").groupby(["date", "stock_id"], as_index=False)
            .first()[["date", "stock_id", "close", "open_interest"]]
            .rename(columns={"close": "near_close", "open_interest": "oi_near_lots"}))

    spots = []
    for f in RAW_SPOT.glob("*.parquet"):
        s = pd.read_parquet(f)
        if not s.empty and "close" in s.columns:
            spots.append(s[["date", "stock_id", "close"]])
    spot = pd.concat(spots, ignore_index=True).rename(columns={"close": "spot_close"})
    spot = spot[spot["spot_close"] > 0]

    # 大額交易人（contract_type=all，個股期貨兩碼代號）
    trd = pd.concat([pd.read_parquet(f) for f in sorted(RAW_TRD.glob("*.parquet"))],
                    ignore_index=True)
    trd = trd[(trd["contract_type"] == "all") & trd["futures_id"].isin(pref_sid)]
    trd["stock_id"] = trd["futures_id"].map(pref_sid)
    trd = trd[["date", "stock_id", "market_open_interest",
               "buy_top10_trader_open_interest_per", "sell_top10_trader_open_interest_per",
               "buy_top10_specific_open_interest", "sell_top10_specific_open_interest"]]
    trd.columns = ["date", "stock_id", "mkt_oi", "buy_top10_pct", "sell_top10_pct",
                   "buy_top10_specific", "sell_top10_specific"]
    # 標準+小型契約各有一列 → 按標的合併：占比以市場 OI 加權、口數相加
    w = trd["mkt_oi"].clip(lower=1)
    trd["_wb"] = trd["buy_top10_pct"] * w
    trd["_ws"] = trd["sell_top10_pct"] * w
    trd = (trd.groupby(["date", "stock_id"], as_index=False)
           .agg(mkt_oi=("mkt_oi", "sum"), _wb=("_wb", "sum"), _ws=("_ws", "sum"),
                buy_top10_specific=("buy_top10_specific", "sum"),
                sell_top10_specific=("sell_top10_specific", "sum")))
    wsum = trd["mkt_oi"].clip(lower=1)
    trd["buy_top10_pct"] = (trd["_wb"] / wsum).round(1)
    trd["sell_top10_pct"] = (trd["_ws"] / wsum).round(1)
    trd = trd.drop(columns=["_wb", "_ws"])

    out = (oi.merge(near, on=["date", "stock_id"], how="left")
             .merge(spot, on=["date", "stock_id"], how="left")
             .merge(trd, on=["date", "stock_id"], how="left"))
    out["basis_pct"] = (out["near_close"] / out["spot_close"] - 1) * 100
    out["top10_skew"] = out["sell_top10_pct"] - out["buy_top10_pct"]
    out.to_parquet(OUT / "stock_futures_oi.parquet", index=False)
    print(f"[build] 完成: {len(out)} 列, {out['stock_id'].nunique()} 檔 "
          f"→ stock_futures_oi.parquet", flush=True)

    # dashboard 快照（最新一日）：{sid: [OI張數等值, 基差%, 大戶偏空skew]}
    import json
    d = out["date"].max()
    snap = out[out["date"] == d]
    rows = {}
    for _, r in snap.iterrows():
        rows[r["stock_id"]] = [
            int(round(r["oi_shares"] / 1000)),                                  # 張數等值
            None if pd.isna(r["basis_pct"]) else round(float(r["basis_pct"]), 2),
            None if pd.isna(r["top10_skew"]) else round(float(r["top10_skew"]), 1),
        ]
    fp = ROOT / "data" / "leverage" / "mkt_futoi.json"
    fp.write_text(json.dumps({"date": d, "source": "TAIFEX via FinMind",
                              "rows": rows}, ensure_ascii=False))
    print(f"[build] dashboard 快照 {d} {len(rows)} 檔 → mkt_futoi.json", flush=True)
    d = out["date"].max()
    last = out[out["date"] == d].nlargest(8, "oi_shares")
    print(last[["date", "stock_id", "oi_std_lots", "oi_mini_lots", "basis_pct",
                "sell_top10_pct", "top10_skew"]].round(2).to_string(index=False), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="all",
                    choices=["all", "futures", "traders", "spot", "build"])
    ap.add_argument("--start", default="2010-01-04")
    ap.add_argument("--out-dir", default=str(OUT))
    args = ap.parse_args()
    _set_paths(Path(args.out_dir), args.start)
    token = _token()
    if args.phase in ("all", "futures"):
        phase_futures(token)
    if args.phase in ("all", "traders"):
        phase_traders(token)
    if args.phase in ("all", "spot"):
        phase_spot(token)
    if args.phase in ("all", "build"):
        build()


if __name__ == "__main__":
    main()
