from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = (
    "你是一個推文摘要助手。你會收到一批推文（含作者、時間、內容、連結），"
    "請用繁體中文整理成重點條列摘要，每一點簡潔扼要，並在每一點後面附上對應的原文連結。"
    "不要捏造推文沒有提到的資訊，也不要輸出多餘的開場白或結語，只要條列摘要本身。"
)


def _format_tweets_for_prompt(tweets: list[dict]) -> str:
    lines = []
    for t in tweets:
        lines.append(f"- 作者: @{t['author']} | 時間: {t['created_at']} | 連結: {t['url']}\n  內容: {t['text']}")
    return "\n".join(lines)


def summarize_group(tweets: list[dict], label: str, api_key: str, model: str) -> str:
    if not tweets:
        return ""

    user_prompt = f"以下是關於「{label}」的推文，請整理成重點條列摘要：\n\n{_format_tweets_for_prompt(tweets)}"

    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()
