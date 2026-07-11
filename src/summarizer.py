from __future__ import annotations

import json
import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# 呼叫 LLM 的逾時與重試。改用串流後，timeout 是「兩個 chunk 之間的閒置上限」，
# 只要生成持續進展就不會觸發；排程不該因單次連線閃斷而整批白跑。
CHAT_TIMEOUT = 120
CHAT_MAX_RETRIES = 4

# 單次摘要最多送幾張圖給視覺模型，控制成本
MAX_IMAGES_PER_GROUP = 12

# 保險：移除模型偶爾會加的「…不納入/略過/與主題無關…」附註（含其中引用的宣傳推文編號），
# 不依賴模型自律。只鎖定明確的「略過」字眼，避免誤刪正當提到訂閱議題的內容。
_SKIP_NOTE_RE = re.compile(
    r"[*＊]?\s*[（(][^（）()]*(不納入|未納入|不列入|略過|故不|不予採|不予納|不予彙整|無關)[^（）()]*[）)]\s*[*＊]?"
)

# 保險：移除「（我）為什麼沒有依產業關係圖延伸推導」這類自我交代句。
_NO_DERIVE_RE = re.compile(
    r"[^。\n]*(不對產業關係圖[^。\n]*(延伸|推導)"
    r"|不(做|進行)[^。\n]*延伸推導"
    r"|圖上[^。\n]*(沒有|無)[^。\n]*相關公司"
    r"|(未|沒有)[^。\n]*(硬體)?供應鏈[^。\n]*(採用|細節)[^。\n]*不(做|對))[^。\n]*。?"
)

# 若上面清空後只剩一個空的「🤖 延伸推論：」開頭，整行移除。
_EMPTY_ROBOT_RE = re.compile(r"^[*＊>\-\s]*🤖\s*延伸推論[:：]?[*＊\s]*$", re.M)


def _strip_skip_notes(summary: str) -> str:
    cleaned = _SKIP_NOTE_RE.sub("", summary)
    cleaned = _NO_DERIVE_RE.sub("", cleaned)
    cleaned = _EMPTY_ROBOT_RE.sub("", cleaned)
    # 收斂被清出來的多餘空行
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()

DEFAULT_SYSTEM_PROMPT = (
    "你是一位協助投資判斷的分析助理。你會收到一段期間內、來自一位或多位追蹤作者（或某個主題）的推文，"
    "每則前面有編號 [n] 並標註作者 @handle。輸入末尾可能附一份「產業關係圖」供你延伸推導。\n"
    "用繁體中文，把推文依【主題／子主題】整理，並把「事實」與「你的推論/看法」分開。\n"
    "每個子主題這樣寫：\n"
    "- 先在子主題標題下【直接寫事實彙整】：客觀整理各作者實際說了什麼、討論了什麼，每個要點附 [n]。"
    "【不要】寫「一、【事實彙整】」這類標題文字，直接寫內容即可。這部分不帶任何投資情緒、看多看空或你的判斷，"
    "只做基於事實的彙整；同一主題有多位作者時可比較說法異同。\n"
    "- 接著，若（且僅若）有值得補充的判斷，另起一段以「🤖 延伸推論：」開頭（不用括號、不用「一、二、」標號），寫入："
    "(a) 依產業關係圖推導的供應鏈關聯（圖上哪些相關公司可能受惠、哪些要留意，沿上下游/競爭關係），"
    "只有當推文事實明確支持、且確實涉及圖上實體時才寫；(b) 你對此主題的情緒/趨勢與投資看法。\n"
    "  ★極重要：若推文內容與產業關係圖無關，就【只寫事實彙整】，該子主題可以完全沒有 🤖 延伸推論段。"
    "【絕對不要】寫「與圖無關」「不做延伸推導」「圖上沒有相關公司」「未提及硬體供應鏈」這類說明——直接省略即可，"
    "不要交代你為什麼沒推導。若連情緒/看法也沒有可靠依據，同樣整段省略。\n"
    "- 【絕對不要】寫「某些推文與主題無關/故不納入/不予彙整」之類的附註；離題、宣傳或你未採用的推文，"
    "直接略過、當作不存在，不要交代。\n"
    "格式與規則：\n"
    "1. 兩層主題階層：大分類用 `## 標題`；子主題用粗體 `**標題**`。子主題比大分類重要，盡量細分；"
    "主題名稱盡量對齊產業關係圖用語。\n"
    "2. 引用作者具體發言時附 [n]；只用實際引用到的編號。\n"
    "3. 絕對不要在內文貼任何網址或連結。\n"
    "4. 若某則推文附有圖片/影片，看懂並在事實彙整中帶入重點；若文中提到某張圖片/影片的內容，"
    "用 [附圖N] 標示（N 為圖片編號），並一樣用 [n] 標推文來源。\n"
    "5. 嚴格排除任何宣傳、行銷、業配、招攬或推銷付費/訂閱服務的內容：主要目的是推銷或吸引訂閱的推文整則忽略，"
    "不摘要、不描述其圖片、不引用其編號。\n"
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


def _consume_stream(resp: requests.Response) -> str:
    """讀取 OpenRouter 的 SSE 串流，把各 chunk 的 delta.content 拼成完整內容。"""
    # SSE 常不帶 charset，requests 會誤用 ISO-8859-1，導致中文被拆成亂碼 → 強制 UTF-8。
    resp.encoding = "utf-8"
    parts: list[str] = []
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or raw.startswith(":"):  # 空行或 keepalive 註解（如 ": OPENROUTER PROCESSING"）
            continue
        if not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if obj.get("error"):
            raise requests.exceptions.HTTPError(f"stream error: {obj['error']}")
        choices = obj.get("choices") or []
        if choices:
            piece = (choices[0].get("delta") or {}).get("content")
            if piece:
                parts.append(piece)
    return "".join(parts)


def _post_chat(api_key: str, payload: dict) -> dict:
    """以串流方式呼叫 OpenRouter chat completions，回傳與非串流相容的 dict。
    對逾時／連線中斷／429／5xx 自動重試（指數退避）；串流下 timeout 只在 chunk 間閒置過久才觸發。"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {**payload, "stream": True}
    last_exc: Exception | None = None
    for attempt in range(1, CHAT_MAX_RETRIES + 1):
        try:
            with requests.post(
                OPENROUTER_URL, headers=headers, json=payload,
                timeout=CHAT_TIMEOUT, stream=True,
            ) as resp:
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.exceptions.HTTPError(f"HTTP {resp.status_code}", response=resp)
                resp.raise_for_status()
                content = _consume_stream(resp)
            if not content.strip():
                raise requests.exceptions.ChunkedEncodingError("串流未回傳任何內容")
            return {"choices": [{"message": {"content": content}}]}
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError, requests.exceptions.HTTPError) as exc:
            last_exc = exc
            if attempt == CHAT_MAX_RETRIES:
                break
            wait = min(2 ** attempt * 5, 60)  # 10s, 20s, 40s...（上限 60s）
            logger.warning("OpenRouter 呼叫失敗（第 %d/%d 次）：%s；%d 秒後重試。",
                           attempt, CHAT_MAX_RETRIES, exc, wait)
            time.sleep(wait)
    raise last_exc  # type: ignore[misc]


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

    data = _post_chat(
        api_key,
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt or DEFAULT_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        },
    )
    summary = _strip_skip_notes(data["choices"][0]["message"]["content"].strip())

    references = [
        {"n": idx, "url": t["url"], "author": t["author"], "media": t.get("media", [])}
        for idx, t in enumerate(tweets, start=1)
    ]
    return {"summary": summary, "references": references}
