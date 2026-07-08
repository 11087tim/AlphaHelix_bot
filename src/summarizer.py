from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = (
    "你是一個推文事件整理助手。你會收到某段時間內的一批推文，每則推文前面都有一個編號 [n]。\n"
    "請用繁體中文，把這段時間內發生的事件、討論與重點，整理成一段連貫、有脈絡的摘要"
    "（可用數個段落或條列，但要像在說明「這段時間發生了什麼」，而不是逐則翻譯）。\n"
    "重要規則：\n"
    "1. 當你的敘述引用到某一則推文的內容時，在該句話後面附上對應的引用標記，例如 [1]、[2]；"
    "若一句話同時對應多則，可寫成 [1][3]。\n"
    "2. 引用標記只用你實際有引用到的編號，數字要對應到輸入的推文編號。\n"
    "3. 絕對不要在內文直接貼出任何網址或連結，連結會由系統依編號自動補上。\n"
    "4. 不要捏造推文沒有提到的資訊，也不要輸出多餘的開場白或結語，只要摘要本身。"
)


def _format_tweets_for_prompt(tweets: list[dict]) -> str:
    lines = []
    for idx, t in enumerate(tweets, start=1):
        lines.append(f"[{idx}] @{t['author']}（{t['created_at']}）：{t['text']}")
    return "\n".join(lines)


def summarize_group(tweets: list[dict], label: str, api_key: str, model: str) -> dict | None:
    """回傳 {"summary": 帶 [n] 標記的摘要文字, "references": [{n, url, author}, ...]}。無推文則回傳 None。"""
    if not tweets:
        return None

    user_prompt = (
        f"以下是關於「{label}」在這段時間內的推文（共 {len(tweets)} 則），"
        f"請整理成一段有脈絡的事件摘要：\n\n{_format_tweets_for_prompt(tweets)}"
    )

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
        timeout=90,
    )
    response.raise_for_status()
    data = response.json()
    summary = data["choices"][0]["message"]["content"].strip()

    references = [
        {"n": idx, "url": t["url"], "author": t["author"]}
        for idx, t in enumerate(tweets, start=1)
    ]
    return {"summary": summary, "references": references}
