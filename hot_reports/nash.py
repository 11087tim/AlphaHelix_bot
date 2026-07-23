"""nash-ai（报告驿站）API client：token 驗證、搜尋、下載。"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from . import config

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


class TokenExpired(Exception):
    """token 失效（微信掃碼登入的 token 有效期 24h，需人工重新登入更新）。"""


class QuotaExhausted(Exception):
    """頁數餘額不足。"""


def login_with_password(phone: str, password: str) -> str:
    """帳密登入換 token。目前微信帳號無法設密碼，此函式備而不用：
    若日後 nash-ai 幫帳號綁定手機+密碼，在 .env 設 NASH_PHONE / NASH_PASSWORD
    即可全自動續 token，不再需要掃碼。"""
    body = json.dumps({"phone": phone, "password": password}).encode()
    req = urllib.request.Request(
        f"{config.NASH_BASE}/reports/auth/login", data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        d = json.load(resp)
    if d.get("code") != 200:
        raise RuntimeError(f"登入失敗：{d.get('message')}")
    return d["data"]


class NashClient:
    def __init__(self, token: str):
        self.token = token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "User-Agent": UA}

    def token_valid(self) -> bool:
        req = urllib.request.Request(
            f"{config.NASH_BASE}/reports/user/info", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp).get("code") == 200
        except Exception as exc:
            logger.warning("token 驗證失敗：%s", exc)
            return False

    def search(self, keyword: str, page_size: int = 10) -> list[dict]:
        body = json.dumps({
            "releaseDate": 0, "startDate": "", "endDate": "", "minPages": 0,
            "keyword": keyword, "reportTypes": [], "industries": [],
            "pageNum": 1, "pageSize": page_size,
        }).encode()
        req = urllib.request.Request(
            f"{config.NASH_BASE}/reports/foreign-rt/search", data=body, method="POST",
            headers={**self._headers(), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.load(resp)
        if d.get("code") == 401:
            raise TokenExpired
        if d.get("code") != 200:
            raise RuntimeError(f"search 失敗：{d}")
        return d["data"]["records"]

    def download_pdf(self, report_id: int) -> bytes:
        """成功回傳 PDF bytes；401 → TokenExpired；400 → QuotaExhausted。"""
        req = urllib.request.Request(
            f"{config.NASH_BASE}/reports/foreign-rt/pdf/download?id={report_id}",
            headers=self._headers())
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "")
        if "pdf" in ctype and data[:4] == b"%PDF":
            return data
        msg = data[:200].decode("utf-8", "ignore")
        if '"code":401' in msg:
            raise TokenExpired
        if '"code":400' in msg:
            raise QuotaExhausted(msg)
        raise RuntimeError(f"下載失敗：{msg}")
