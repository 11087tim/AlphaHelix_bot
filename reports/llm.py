from __future__ import annotations

import os

import requests
from dotenv import load_dotenv

from .config import PROJECT_ROOT

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def get_api_key() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("缺少 OPENROUTER_API_KEY（請在 .env 設定）。")
    return key


def chat(model: str, system: str, user: str, api_key: str, timeout: int = 180) -> dict:
    """呼叫 OpenRouter，回傳 {text, prompt_tokens, completion_tokens, cost}。"""
    resp = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "usage": {"include": True},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    usage = data.get("usage", {}) or {}
    return {
        "text": data["choices"][0]["message"]["content"].strip(),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cost": usage.get("cost"),
    }
