from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    accounts: list[str]
    keywords: list[str]
    max_results_per_source: int
    fetch_window_hours: float
    media_enabled: bool
    media_describe: bool
    openrouter_model: str
    openrouter_system_prompt: str
    memory_model: str
    openrouter_api_key: str
    site_title: str
    site_output_dir: Path
    site_url: str
    site_auto_push: bool
    email_to: list[str]
    email_subject_prefix: str
    gmail_address: str
    gmail_app_password: str
    x_bearer_token: str


class ConfigError(RuntimeError):
    pass


def load_config(config_path: Path | None = None) -> Config:
    load_dotenv(PROJECT_ROOT / ".env")

    config_path = config_path or PROJECT_ROOT / "config.yaml"
    if not config_path.exists():
        raise ConfigError(f"找不到設定檔: {config_path}")

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    x_bearer_token = os.environ.get("X_BEARER_TOKEN", "")
    openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "")
    gmail_address = os.environ.get("GMAIL_ADDRESS", "")
    # Gmail 應用程式密碼常被複製成 "xxxx xxxx xxxx xxxx" 格式，去掉空格避免登入失敗
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")

    missing = [
        name
        for name, value in [
            ("X_BEARER_TOKEN", x_bearer_token),
            ("OPENROUTER_API_KEY", openrouter_api_key),
            ("GMAIL_ADDRESS", gmail_address),
            ("GMAIL_APP_PASSWORD", gmail_app_password),
        ]
        if not value
    ]
    if missing:
        raise ConfigError(
            "缺少必要的環境變數: "
            + ", ".join(missing)
            + "。請複製 .env.example 為 .env 並填入對應金鑰。"
        )

    accounts = raw.get("accounts") or []
    keywords = raw.get("keywords") or []
    if not accounts and not keywords:
        raise ConfigError("config.yaml 裡的 accounts 與 keywords 都是空的，至少要設定一項。")

    site = raw.get("site") or {}
    email = raw.get("email") or {}
    openrouter = raw.get("openrouter") or {}

    # email.to 支援單一字串或字串清單，統一正規化成 list
    raw_to = email.get("to", "")
    if isinstance(raw_to, str):
        email_to = [addr.strip() for addr in raw_to.split(",") if addr.strip()]
    else:
        email_to = [str(addr).strip() for addr in raw_to if str(addr).strip()]

    return Config(
        accounts=accounts,
        keywords=keywords,
        max_results_per_source=raw.get("max_results_per_source", 10),
        fetch_window_hours=raw.get("fetch_window_hours", 1),
        media_enabled=(raw.get("media") or {}).get("enabled", True),
        media_describe=(raw.get("media") or {}).get("describe", False),
        openrouter_model=openrouter.get("model", "anthropic/claude-3.5-haiku"),
        openrouter_system_prompt=(openrouter.get("system_prompt") or "").strip(),
        # 跨時間記憶的立場萃取模型：需判斷/校準但量小，用 Sonnet 較穩、成本可忽略
        memory_model=openrouter.get("memory_model", "anthropic/claude-sonnet-5"),
        openrouter_api_key=openrouter_api_key,
        site_title=site.get("title", "我的 X 摘要"),
        site_output_dir=PROJECT_ROOT / site.get("output_dir", "docs"),
        site_url=site.get("url", ""),
        site_auto_push=site.get("auto_push", True),
        email_to=email_to,
        email_subject_prefix=email.get("subject_prefix", "[X Digest]"),
        gmail_address=gmail_address,
        gmail_app_password=gmail_app_password,
        x_bearer_token=x_bearer_token,
    )
