"""熱門外資研報 pipeline 設定（valuelist 熱榜 → nash-ai 下載 → LLM 彙整）。"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "hot_reports_data"
PDF_DIR = DATA_DIR / "pdf"
TEXT_DIR = DATA_DIR / "text"
DIGEST_DIR = DATA_DIR / "digest"
STATE_PATH = DATA_DIR / "state.json"
TOKEN_PATH = DATA_DIR / "token.txt"

VALUELIST_URL = "https://www.valuelist.cn/hot-report"
NASH_BASE = "https://www.nash-ai.cn"

# LLM（走 OpenRouter，key 沿用 .env 的 OPENROUTER_API_KEY）
SUMMARY_MODEL = os.environ.get("HOT_REPORTS_SUMMARY_MODEL", "anthropic/claude-sonnet-5")
SYNTH_MODEL = os.environ.get("HOT_REPORTS_SYNTH_MODEL", "anthropic/claude-opus-4.8")
MAX_TEXT_CHARS = 150_000          # 單篇餵給 LLM 的文字上限

MATCH_THRESHOLD = 0.85            # 標題匹配高信心門檻
SEARCH_DELAY_SEC = 0.8
DOWNLOAD_DELAY_SEC = 2.0


def ensure_dirs() -> None:
    for d in (DATA_DIR, PDF_DIR, TEXT_DIR, DIGEST_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_email_cfg() -> dict:
    """沿用 X bot config.yaml 的 email 區塊（prod/dev 收件人、寄件帳號在 .env）。"""
    load_dotenv(PROJECT_ROOT / ".env")
    cfg = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
    email = cfg.get("email", {}) or {}
    return {
        "gmail_address": os.environ.get("GMAIL_ADDRESS", ""),
        "gmail_app_password": os.environ.get("GMAIL_APP_PASSWORD", ""),
        "prod": email.get("prod", []) or [],
        "dev": email.get("dev", []) or [],
    }


def read_token() -> str:
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text().strip()
    return ""
