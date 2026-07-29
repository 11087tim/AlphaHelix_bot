"""台股重大訊息每日收集器（上市 TWSE OpenAPI + 上櫃 TPEx OpenAPI）。

兩個 API 都是「當日快照」、無歷史查詢——不收就永久缺洞，故設計成 cron 天天跑：
    python -m reports.announcements
全市場都存（一天 <1MB），累積成本地歷史庫 data/announcements/YYYY-MM.jsonl，
之後任何新追蹤標的都有回溯資料。獨立執行、不讀 reports_config.yaml（VM 上沒有該檔）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import date
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"
STORE_DIR = Path(__file__).resolve().parent.parent / "data" / "announcements"


def _roc_date(s: str) -> str:
    """1150727 → 2026-07-27；解析失敗回原字串。"""
    s = (s or "").strip()
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}"
    return s


def _roc_time(s: str) -> str:
    """65627 → 06:56:27。"""
    s = (s or "").strip().zfill(6)
    return f"{s[:2]}:{s[2:4]}:{s[4:6]}" if s.isdigit() else s


def _normalize(row: dict, market: str) -> dict:
    get = lambda *keys: next((str(row[k]).strip() for k in keys if k in row and row[k]), "")
    rec = {
        "date": _roc_date(get("發言日期")),
        "time": _roc_time(get("發言時間")),
        "stock_id": get("公司代號", "SecuritiesCompanyCode"),
        "company": get("公司名稱", "CompanyName"),
        "subject": get("主旨 ", "主旨"),
        "clause": get("符合條款"),
        "fact_date": _roc_date(get("事實發生日")),
        "description": get("說明"),
        "market": market,
    }
    rec["key"] = hashlib.sha1(
        f"{rec['stock_id']}|{rec['date']}|{rec['time']}|{rec['subject']}".encode()
    ).hexdigest()[:16]
    return rec


def _load_seen_keys() -> set[str]:
    """讀最近兩個月檔案的 key（跨月交界不重複收）。"""
    seen = set()
    for p in sorted(STORE_DIR.glob("*.jsonl"))[-2:]:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["key"])
            except (json.JSONDecodeError, KeyError):
                continue
    return seen


def collect() -> int:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    records = []
    for url, market in ((TWSE_URL, "twse"), (TPEX_URL, "tpex")):
        try:
            resp = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            rows = resp.json()
            records += [_normalize(r, market) for r in rows]
            logger.info("%s 取得 %d 筆", market, len(rows))
        except Exception as exc:  # noqa: BLE001 — 單邊失敗不影響另一邊
            logger.error("%s 抓取失敗：%s", market, exc)

    seen = _load_seen_keys()
    fresh = [r for r in records if r["key"] not in seen]
    # 依公告的發言日期歸檔（跨日補收時歸對月份）
    by_month: dict[str, list[dict]] = {}
    for r in fresh:
        month = r["date"][:7] if len(r["date"]) >= 7 else date.today().strftime("%Y-%m")
        by_month.setdefault(month, []).append(r)
    for month, rows in sorted(by_month.items()):
        out = STORE_DIR / f"{month}.jsonl"
        with out.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    logger.info("新增 %d 筆（重複略過 %d），存於 %s", len(fresh), len(records) - len(fresh), STORE_DIR)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    sys.exit(collect())
