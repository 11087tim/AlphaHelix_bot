from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# 單次摘要最多送幾張圖給視覺模型，控制成本
MAX_IMAGES_PER_GROUP = 12

SYSTEM_PROMPT = (
    "你是一個推文事件整理助手。你會收到某段時間內的一批推文，每則推文前面都有一個編號 [n]。\n"
    "請用繁體中文，把這段時間內發生的事件、討論與重點，整理成一段連貫、有脈絡的摘要"
    "（可用數個段落或條列，但要像在說明「這段時間發生了什麼」，而不是逐則翻譯）。\n"
    "重要規則：\n"
    "1. 當你的敘述引用到某一則推文的內容時，在該句話後面附上對應的引用標記，例如 [1]、[2]；"
    "若一句話同時對應多則，可寫成 [1][3]。\n"
    "2. 引用標記只用你實際有引用到的編號，數字要對應到輸入的推文編號。\n"
    "3. 絕對不要在內文直接貼出任何網址或連結，連結會由系統依編號自動補上。\n"
    "4. 若某則推文附有圖片，圖片會緊接在該則推文文字後面提供給你。請看懂圖片內容，"
    "並在摘要中自然地描述與帶入重點（例如圖表數據、截圖重點、示意圖等），一樣用 [n] 標記來源。\n"
    "5. 不要捏造推文或圖片沒有的資訊，也不要輸出多餘的開場白或結語，只要摘要本身。"
)

_vision_cache: dict[str, bool] = {}


def model_supports_vision(model: str, api_key: str) -> bool:
    """查詢 OpenRouter 該模型是否支援圖片輸入（結果快取於記憶體）。"""
    if model in _vision_cache:
        return _vision_cache[model]
    supports = False
    try:
        resp = requests.get(
            OPENROUTER_MODELS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        resp.raise_for_status()
        for m in resp.json().get("data", []):
            if m.get("id") == model:
                modalities = (m.get("architecture") or {}).get("input_modalities", [])
                supports = "image" in modalities
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning("查詢模型視覺能力失敗，暫不送圖片：%s", exc)
    _vision_cache[model] = supports
    return supports


def _format_tweets_for_prompt(tweets: list[dict]) -> str:
    lines = []
    for idx, t in enumerate(tweets, start=1):
        lines.append(f"[{idx}] @{t['author']}（{t['created_at']}）：{t['text']}")
    return "\n".join(lines)


def _build_multimodal_content(label: str, tweets: list[dict]) -> list[dict]:
    """把推文與其圖片交錯成多模態 content：每則推文文字後面接上該則的圖片。"""
    content: list[dict] = [
        {
            "type": "text",
            "text": f"以下是關於「{label}」在這段時間內的推文（共 {len(tweets)} 則），請整理成一段有脈絡的事件摘要：",
        }
    ]
    image_budget = MAX_IMAGES_PER_GROUP
    for idx, t in enumerate(tweets, start=1):
        content.append({"type": "text", "text": f"[{idx}] @{t['author']}（{t['created_at']}）：{t['text']}"})
        for m in t.get("media", []):
            if image_budget <= 0:
                break
            url = m.get("image_url")
            if not url:
                continue
            label_txt = "（此推文附影片，以下為影片畫面縮圖）" if m.get("type") != "photo" else "（此推文附圖片）"
            content.append({"type": "text", "text": f"[{idx}] {label_txt}"})
            content.append({"type": "image_url", "image_url": {"url": url}})
            image_budget -= 1
    return content


def summarize_group(
    tweets: list[dict],
    label: str,
    api_key: str,
    model: str,
    describe_media: bool = False,
) -> dict | None:
    """回傳 {"summary": 帶 [n] 標記的摘要文字, "references": [{n, url, author, media}, ...]}。無推文則回傳 None。"""
    if not tweets:
        return None

    has_images = any(m.get("image_url") for t in tweets for m in t.get("media", []))
    if describe_media and has_images:
        user_content: object = _build_multimodal_content(label, tweets)
    else:
        user_content = (
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
                {"role": "user", "content": user_content},
            ],
        },
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    summary = data["choices"][0]["message"]["content"].strip()

    references = [
        {"n": idx, "url": t["url"], "author": t["author"], "media": t.get("media", [])}
        for idx, t in enumerate(tweets, start=1)
    ]
    return {"summary": summary, "references": references}
