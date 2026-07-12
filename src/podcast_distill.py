"""把長訪談逐字稿蒸餾成「投資要點 items」（形狀同推文），供餵進 digest 合成。

逐字稿 90% 是口語/寒暄/贊助，直接丟合成會爆量又稀釋訊號；先在此壓成幾條一手、前瞻性的要點。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MAX_TRANSCRIPT_CHARS = 300000  # 上限（Sonnet 長脈絡夠；涵蓋 Acquired 這類 3–4 小時長節目）

_SYSTEM = (
    "你是投資分析助理。以下是一集科技/財經 podcast 或訪談的逐字稿（含口語、可能有贊助與閒聊）。"
    "抽出其中對投資判斷真正有價值的重點，特別是【前瞻/領先訊號】：公司策略、需求強弱、產能與資本支出、"
    "競爭態勢、供應鏈、guidance 或高層語氣、技術路線轉變。每點標明是誰說的（主持人/來賓/某公司高層姓名或職稱）。"
    "【忽略】贊助業配、寒暄、與投資無關的閒聊。用繁體中文，每點一句話、具體、可對應到公司或主題。"
    "輸出 JSON 陣列，每筆 {\"speaker\": 說話者, \"point\": 一句話重點}；8~15 點為宜，寧缺勿濫、不要硬湊。"
    "只輸出 JSON 陣列。"
)


def distill(episode: dict, transcript: str, model: str, api_key: str) -> list[dict]:
    """回傳投資要點 items（形狀同推文：id/author/text/created_at/url/source/media）。"""
    transcript = (transcript or "").strip()
    if not transcript:
        return []
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:MAX_TRANSCRIPT_CHARS]

    show = episode.get("show", "Podcast")
    title = episode.get("title", "")
    user = f"節目：{show}\n本集：{title}\n\n逐字稿：\n{transcript}"
    try:
        from . import summarizer
        from .memory_extract import _parse_json_array
        data = summarizer._post_chat(api_key, {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
        })
        points = _parse_json_array(data["choices"][0]["message"]["content"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("蒸餾失敗，略過本集：%s", exc)
        return []

    published = episode.get("published")
    created_at = published.strftime("%Y-%m-%d %H:%M") if published else ""
    url = episode.get("page_url") or episode.get("audio_url", "")
    show_label = f"🎙️{show}"
    items: list[dict] = []
    for i, p in enumerate(points, 1):
        if not isinstance(p, dict):
            continue
        point = str(p.get("point", "")).strip()
        if not point:
            continue
        speaker = str(p.get("speaker", "")).strip()
        text = f"（{title}）{speaker}：{point}" if speaker else f"（{title}）{point}"
        items.append({
            "id": f"{episode.get('id','ep')}#{i}",
            "author": show_label,
            "text": text,
            "created_at": created_at,
            "url": url,
            "source": f"podcast:{show}",
            "media": [],
        })
    logger.info("蒸餾「%s」→ %d 條要點", title[:30], len(items))
    return items
