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
    "每則前面有編號 [n] 並標註作者 @handle。輸入末尾可能附一份「產業關係圖」供你延伸推導。\n"
    "你的任務不是逐則摘要，而是【依主題／子主題彙整、判斷情緒與趨勢、並延伸供應鏈關聯】，"
    "用繁體中文產出對投資有意義的洞察。\n"
    "做法：\n"
    "- 先把貼文依「主題／子主題」歸類（例如 光通訊/CPO、記憶體/HBM），每個主題一段。子主題比大分類更重要，請盡量細分。\n"
    "- 每個主題下：(a) 說明核心觀點與理由；(b) 點出當前的【討論熱度與情緒傾向】"
    "（偏多/偏空/分歧，以及相較先前是升溫還是降溫，若無從判斷就說明）；(c) 加入你的評斷（對投資的意涵、機會或風險訊號）。\n"
    "- 同一主題有多位作者才比較立場、指出異同；只有單一作者就直接呈現，不要寫「無人反駁」這類話，也不要為湊比較而杜撰。\n"
    "- 【運用產業關係圖延伸】：若討論明確涉及圖上的主題/公司，可依圖中的上下游/競爭關係，推導『誰可能受惠、誰需要留意』。"
    "但務必嚴格【基於推文實際內容】：只有當討論的事實明確支持該推論時才寫；圖上沒有的關係、或推文沒提到的，一律不要臆測或補寫。"
    "寧可不寫，也不要編。這類推導請用「（延伸推論：…）」標明，與推文事實清楚區隔。\n"
    "格式與規則：\n"
    "1. 用兩層主題階層，皆獨立成行：大分類用 Markdown 的 `## 標題`；細分子主題用粗體 `**標題**`。"
    "主題名稱盡量對齊產業關係圖上的用語。每個主題下再展開分析，不要逐則流水帳。\n"
    "2. 引用作者具體發言時，在該句後附上引用標記 [n]；一句對應多則可寫成 [1][3]。\n"
    "3. 只使用你實際引用到的編號，數字需對應輸入的推文編號。\n"
    "4. 絕對不要在內文貼任何網址或連結，連結會由系統依編號自動補上。\n"
    "5. 若某則推文附有圖片，圖片會緊接在該則文字後面提供。請看懂圖片並在分析中帶入重點，一樣用 [n] 標記來源。\n"
    "6. 嚴格排除任何宣傳、行銷、業配、招攬或推銷付費/訂閱服務的內容：主要目的是推銷或吸引訂閱的推文整則忽略，"
    "不摘要、不描述其圖片、不引用其編號，也不要加「某則在推銷故不納入」這類附註。\n"
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
    graph_context: str | None = None,
) -> dict | None:
    """回傳 {"summary": 帶 [n] 標記的摘要文字, "references": [{n, url, author, media}, ...]}。無推文則回傳 None。"""
    if not tweets:
        return None

    intro = (
        f"以下是「{label}」相關、來自多位作者在這段期間的推文（共 {len(tweets)} 則）。"
        f"請依你被賦予的規則，做跨作者的觀點彙整與評斷（依主題／子主題組織、指出異同、加入判斷）："
    )
    has_images = any(m.get("image_url") for t in tweets for m in t.get("media", []))
    if describe_media and has_images:
        content = _build_multimodal_content(label, tweets)
        if graph_context:
            content.append({"type": "text", "text": graph_context})
        user_content: object = content
    else:
        user_content = f"{intro}\n\n{_format_tweets_for_prompt(tweets)}"
        if graph_context:
            user_content += f"\n\n{graph_context}"

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
