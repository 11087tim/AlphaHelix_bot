from __future__ import annotations

import logging
import re

import requests

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# 單次摘要最多送幾張圖給視覺模型，控制成本
MAX_IMAGES_PER_GROUP = 12

# 保險：移除模型偶爾會加的「…不納入/略過…」附註（含其中引用的宣傳推文編號），
# 不依賴模型自律。只鎖定明確的「略過」字眼，避免誤刪正當提到訂閱議題的內容。
_SKIP_NOTE_RE = re.compile(
    r"[*＊]?\s*[（(][^（）()]*(不納入|未納入|不列入|略過|故不|不予採|不予納)[^（）()]*[）)]\s*[*＊]?"
)


def _strip_skip_notes(summary: str) -> str:
    cleaned = _SKIP_NOTE_RE.sub("", summary)
    # 收斂被清出來的多餘空行
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()

DEFAULT_SYSTEM_PROMPT = (
    "你是一位協助投資判斷的分析助理。你會收到一段期間內、來自一位或多位追蹤作者（或某個主題）的推文，"
    "每則前面有編號 [n] 並標註作者 @handle。\n"
    "你的任務不是逐則摘要，而是【依主題彙整並加入評斷】，用繁體中文產出對投資有意義的洞察。\n"
    "做法：\n"
    "- 先把貼文依「主題／觀點」歸類，每個主題一段。\n"
    "- 每個主題下：說明核心觀點與理由，並加入你的評斷（對投資或決策的意涵、機會或風險訊號）。\n"
    "- 只有當『同一個主題』有兩位以上作者發言時，才比較他們的立場、指出彼此同意或分歧之處。"
    "若某主題只有單一作者，就直接呈現並評斷該觀點，不要寫「沒有其他作者對照」「無人反駁」這類話，"
    "也不要為了湊比較而杜撰他人立場或虛構分歧。\n"
    "格式與規則：\n"
    "1. 以「主題」作小標，獨立成行、用 Markdown 粗體 **標題**（或 ### 標題），每個主題下再展開分析，不要逐則流水帳。\n"
    "2. 引用作者具體發言時，在該句後附上引用標記 [n]（對應輸入編號）；一句對應多則可寫成 [1][3]。\n"
    "3. 只使用你實際引用到的編號，數字需對應輸入的推文編號。\n"
    "4. 絕對不要在內文貼任何網址或連結，連結會由系統依編號自動補上。\n"
    "5. 若某則推文附有圖片，圖片會緊接在該則文字後面提供。請看懂圖片並在分析中帶入重點"
    "（例如圖表數據、截圖內容、示意圖等），一樣用 [n] 標記來源。\n"
    "6. 嚴格排除任何宣傳、行銷、廣告、業配、招攬或推銷付費/訂閱服務的內容：只要一則推文的主要目的是"
    "推銷或吸引訂閱（例如「訂閱者才看得到」的預告式貼文），就整則完全忽略——不摘要、不描述其圖片、"
    "不引用其編號，也不要提到任何訂閱服務名稱或叫人訂閱的訊息，更不要加「某則在推銷故不納入」這類附註。\n"
    "7. 不要捏造推文或圖片沒有的資訊，不要多餘的開場白或結語，只輸出彙整分析本身。"
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
            "text": f"以下是「{label}」相關、來自多位作者在這段期間的推文（共 {len(tweets)} 則）。"
                    "請依你被賦予的規則，做跨作者的觀點彙整與評斷（依主題組織、指出異同、加入判斷）：",
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
    system_prompt: str | None = None,
) -> dict | None:
    """回傳 {"summary": 帶 [n] 標記的摘要文字, "references": [{n, url, author, media}, ...]}。無推文則回傳 None。"""
    if not tweets:
        return None

    has_images = any(m.get("image_url") for t in tweets for m in t.get("media", []))
    if describe_media and has_images:
        user_content: object = _build_multimodal_content(label, tweets)
    else:
        user_content = (
            f"以下是「{label}」相關、來自多位作者在這段期間的推文（共 {len(tweets)} 則）。"
            f"請依你被賦予的規則，做跨作者的觀點彙整與評斷（依主題組織、指出異同、加入判斷）："
            f"\n\n{_format_tweets_for_prompt(tweets)}"
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
                {"role": "system", "content": system_prompt or DEFAULT_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        },
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    summary = _strip_skip_notes(data["choices"][0]["message"]["content"].strip())

    references = [
        {"n": idx, "url": t["url"], "author": t["author"], "media": t.get("media", [])}
        for idx, t in enumerate(tweets, start=1)
    ]
    return {"summary": summary, "references": references}
