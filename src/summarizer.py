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
    "用繁體中文，把推文依【主題／子主題】整理，並把「事實」與「你的推論/看法」清楚分開。\n"
    "每個子主題請寫成兩部分：\n"
    "一、【事實彙整】：客觀整理各作者實際說了什麼、討論了哪些內容，依事實陳述、每個要點附 [n]。"
    "這一段【不要】帶入任何投資情緒、看多看空或你的判斷，只做基於事實的彙整。"
    "同一主題有多位作者時，可比較他們說法的異同（這也是事實層面）。\n"
    "二、【推論與看法】：另起一段，以「🤖 延伸推論：」開頭（不要用括號），在這一段才寫入你的判斷，包含：\n"
    "  (a) 依產業關係圖推導的供應鏈關聯——這則消息/主題，圖上哪些相關公司可能受惠、哪些要留意"
    "（沿上下游/競爭關係）。務必嚴格基於推文事實：只有討論明確支持時才推；圖上沒有的關係、或推文沒提到的，"
    "一律不寫，寧可不寫也不要編。\n"
    "  (b) 你對此主題的情緒/趨勢判斷（偏多/偏空/分歧、相較先前升溫或降溫）與投資看法。\n"
    "  若某子主題實在沒有可靠的推論或看法，這段可省略，不要硬寫。\n"
    "格式與規則：\n"
    "1. 兩層主題階層：大分類用 `## 標題`；子主題用粗體 `**標題**`。子主題比大分類重要，盡量細分；"
    "主題名稱盡量對齊產業關係圖用語。\n"
    "2. 引用作者具體發言時附 [n]；只用實際引用到的編號。\n"
    "3. 絕對不要在內文貼任何網址或連結。\n"
    "4. 若某則推文附有圖片/影片，看懂並在【事實彙整】中帶入重點；若文中提到某張圖片/影片的內容，"
    "用 [附圖N] 標示（N 為圖片編號），並一樣用 [n] 標推文來源。\n"
    "5. 嚴格排除任何宣傳、行銷、業配、招攬或推銷付費/訂閱服務的內容：主要目的是推銷或吸引訂閱的推文整則忽略，"
    "不摘要、不描述其圖片、不引用其編號，也不要加「某則在推銷故不納入」這類附註。\n"
    "6. 不要捏造推文或圖片沒有的資訊，不要多餘的開場白或結語，只輸出彙整本身。"
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


def _assign_figures(tweets: list[dict]) -> int:
    """依推文順序給每張圖片編號（fig_no），供內文以 [附圖N] 引用與顯示對照。"""
    k = 0
    for t in tweets:
        for m in t.get("media", []):
            if m.get("image_url"):
                k += 1
                m["fig_no"] = k
    return k


def _build_multimodal_content(label: str, tweets: list[dict]) -> list[dict]:
    """把推文與其圖片交錯成多模態 content：每則推文文字後面接上該則的圖片（含 [附圖N] 標號）。"""
    content: list[dict] = [
        {
            "type": "text",
            "text": f"以下是「{label}」相關、來自多位作者在這段期間的推文（共 {len(tweets)} 則）。"
                    "請依你被賦予的規則彙整；若在文中提到某張圖片/影片內容，用 [附圖N] 標示（N 見圖片標號）：",
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
            kind = "影片畫面" if m.get("type") != "photo" else "圖片"
            content.append({"type": "text", "text": f"附圖{m.get('fig_no')}（來自推文[{idx}]，{kind}）："})
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

    _assign_figures(tweets)  # 給圖片編號（供 [附圖N] 引用與顯示對照）
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
