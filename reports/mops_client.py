from __future__ import annotations

import logging
import random
import re
import threading
import time

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://doc.twse.com.tw/server-java/t57sb01"
FILE_HOST = "https://doc.twse.com.tw"

# (財報類型, 語言) → MOPS 檔名代碼
#   AI1=中文合併, AI3=中文個體, AIA=英文合併, AIC=英文個體
TYPE_CODE = {
    ("consolidated", "zh"): "AI1",
    ("individual", "zh"): "AI3",
    ("consolidated", "en"): "AIA",
    ("individual", "en"): "AIC",
}

_PDF_LINK_RE = re.compile(r"/pdf/[^\s\"'<>]+\.pdf")


class MopsError(RuntimeError):
    pass


class MopsClient:
    """公開資訊觀測站(doc.twse.com.tw) 財報下載客戶端。
    三步：list_year(列該年檔案) → resolve+download(step9 產生連結後抓 PDF)。
    含全域限速（跨執行緒共用）與失敗指數退避重試。"""

    def __init__(self, min_interval_sec: float = 0.4, max_retries: int = 3):
        # 每個執行緒各自一個 session：MOPS 的 step9 依賴 step1 建立的 session 狀態，
        # 共用 session 會讓並行的查詢互相干擾。限速鎖則全域共用。
        self._tls = threading.local()
        self.min_interval = min_interval_sec
        self.max_retries = max_retries
        self._lock = threading.Lock()
        self._last_request = 0.0

    @property
    def session(self) -> requests.Session:
        s = getattr(self._tls, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://doc.twse.com.tw/"})
            self._tls.session = s
        return s

    def _throttle(self) -> None:
        """全域限速：確保任兩次對 MOPS 的請求間隔 >= min_interval。"""
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        last_exc = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.request(method, url, timeout=90, **kwargs)
                if resp.status_code == 200:
                    return resp
                last_exc = MopsError(f"HTTP {resp.status_code}")
            except requests.RequestException as exc:
                last_exc = exc
            # 指數退避 + 抖動
            time.sleep((2 ** attempt) * 0.6 + random.uniform(0, 0.4))
        raise MopsError(f"請求失敗（重試 {self.max_retries} 次）：{url}：{last_exc}")

    def list_year(self, co_id: str, ad_year: int) -> list[str]:
        """列出某公司某年度（西元）所有財報檔名（含四季）。一次查詢涵蓋整年。"""
        roc_year = ad_year - 1911
        resp = self._request(
            "POST",
            BASE_URL,
            data={"step": "1", "colorchg": "", "seamon": "", "mtype": "A",
                  "co_id": co_id, "year": str(roc_year)},
        )
        resp.encoding = "big5"
        return re.findall(rf'readfile2\("[A-Z]","{re.escape(co_id)}","([^"]+\.pdf)"\)', resp.text)

    def download(self, co_id: str, filename: str) -> bytes:
        """對指定檔名做 step9 取得暫時連結，再下載 PDF bytes。
        step9 偶爾不回連結（需先 step1 重建 session 狀態），故重試數次。"""
        link = None
        for attempt in range(self.max_retries):
            resp = self._request(
                "POST",
                BASE_URL,
                data={"step": "9", "kind": "A", "co_id": co_id, "filename": filename},
            )
            resp.encoding = "big5"
            m = _PDF_LINK_RE.search(resp.text)
            if m:
                link = m.group(0)
                break
            # 重建 session 狀態：重新 step1 該公司該年，再試 step9
            year = int(filename[:4])
            self.list_year(co_id, year)
        if not link:
            raise MopsError(f"step9 未取得 PDF 連結：{filename}")

        pdf = self._request("GET", FILE_HOST + link)
        if pdf.content[:4] != b"%PDF":
            raise MopsError(f"下載內容不是 PDF：{filename}")
        return pdf.content

    def pick_filename(self, files: list[str], co_id: str, ad_year: int, quarter: int,
                      report_types: list[str], language: str) -> tuple[str | None, str | None]:
        """從清單挑出符合（年/季/類型偏好/語言）的檔名。回傳 (filename, report_type)。"""
        prefix = f"{ad_year}{quarter:02d}_{co_id}_"
        for rt in report_types:
            code = TYPE_CODE.get((rt, language))
            if not code:
                continue
            fn = f"{prefix}{code}.pdf"
            if fn in files:
                return fn, rt
        return None, None
