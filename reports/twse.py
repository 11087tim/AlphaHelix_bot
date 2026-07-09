from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

# 上市月營收資料集含 公司名稱 + 產業別（免金鑰）
_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
_cache: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    global _cache
    if _cache is None:
        _cache = {}
        try:
            r = requests.get(_URL, timeout=30)
            r.raise_for_status()
            for row in r.json():
                code = row.get("公司代號")
                if code and code not in _cache:
                    _cache[code] = {
                        "name": row.get("公司名稱", ""),
                        "industry": row.get("產業別", ""),
                    }
        except Exception as exc:  # noqa: BLE001
            logger.warning("查詢 TWSE 公司資料失敗（將無產業別）：%s", exc)
    return _cache


def get_company_info(stock: str) -> dict:
    return _load().get(stock, {"name": "", "industry": ""})
