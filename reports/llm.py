from __future__ import annotations

import os
import random
import time

import requests
from dotenv import load_dotenv

from .config import PROJECT_ROOT

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_RETRYABLE = {429, 500, 502, 503, 529}


def get_api_key() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("缺少 OPENROUTER_API_KEY（請在 .env 設定）。")
    return key


def _post(model: str, messages: list, api_key: str, timeout: int, retries: int = 4) -> dict:
    last = None
    for attempt in range(retries):
        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "usage": {"include": True}},
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                usage = data.get("usage", {}) or {}
                return {
                    "text": data["choices"][0]["message"]["content"].strip(),
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "cost": usage.get("cost"),
                }
            last = f"HTTP {resp.status_code}: {resp.text[:180]}"
            if resp.status_code not in _RETRYABLE:
                break
        except requests.RequestException as exc:
            last = str(exc)
        time.sleep((2 ** attempt) * 1.5 + random.uniform(0, 1))
    raise RuntimeError(f"OpenRouter 呼叫失敗（{model}）：{last}")


def chat(model: str, system: str, user: str, api_key: str, timeout: int = 180) -> dict:
    """純文字呼叫。回傳 {text, prompt_tokens, completion_tokens, cost}。"""
    return _post(model, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], api_key, timeout)


def vision_chat(model: str, system: str, user: str, images_b64: list[str],
                api_key: str, timeout: int = 240) -> dict:
    """多模態呼叫：把數張頁面圖片（base64 PNG）連同指示送出。"""
    content = [{"type": "text", "text": user}]
    for b64 in images_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    return _post(model, [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ], api_key, timeout)
