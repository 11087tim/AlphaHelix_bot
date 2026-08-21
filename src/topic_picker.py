"""深查議題挑選器（跨源印證 v2 的上游）：從當期 digest 挑出 0~N 個值得深入查證的議題。

目標函數：深查價值 = 影響力 × 不確定性 × 可查證性——任一項為零，整題價值就是零。
quota 是上限不是目標：沒有夠格的議題就輸出 0 題（分數門檻與 claim 必填在程式端強制執行，
不靠模型自律）。挑題是評分排序任務、不是深度推理，用便宜模型（memory_model）。

深查引擎（Opus 5 + web search）接上前，挑題結果先展示於 digest 供觀察挑題品質。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 3     # 每期挑題上限
MIN_TOTAL = 17         # 五維總分（滿分 25）門檻，低於此視為不夠格，寧缺勿濫
DEDUP_DIGESTS = 20     # 去重回看最近幾份 digest（每天 2 份 → 約 10 天）
SIM_THRESHOLD = 0.55   # claim 相似度（字元 bigram Jaccard）超過此值視為近日已挑過
SCORE_KEYS = ("relevance", "verifiability", "uncertainty", "resonance", "imbalance")

_SYSTEM_PROMPT = """你是投資情報系統的「深查議題挑選器」。系統每天只對極少數議題做高成本的深入查證
（溯源找原始出處、多來源交叉比對、區分事實/估計/傳聞、供應鏈框架分析），挑題品質決定整條鏈的價值。
請讀入當期已彙整的觀點 digest，挑出最多 {max_n} 個值得深查的議題。

一個議題的深查價值 = 影響力 × 不確定性 × 可查證性，任一項為零則整題為零：
- 已被官方證實的重大消息不用查（沒有不確定性）；與關注實體無關的消息不用查（沒有影響力）；
- 純觀點（如「估值太高」「AI 是泡沫」）查不動，不收。每題必須寫出一句具體、可被證實或
  推翻的宣稱（claim），含數字、時程、文件、明確事件者佳。

每題依五個維度給 1~5 分：
- relevance 實體相關性：涉及【持股】實體給高分，【關注】實體次之，供應鏈鄰居再次之
- verifiability 可查證性：claim 越具體、越可能找到一手來源（官方文件/財報/原始報告）分數越高
- uncertainty 不確定性：單一來源傳聞、各方說法矛盾、來源自己標注「傳聞/據傳」者高分
- resonance 跨源共振：多個獨立來源（X、Podcast、YouTube、文章）同時在講者高分
- imbalance 反應失衡：市場或輿論反應強度明顯大於證據強度者高分（查證超額報酬所在）；
  若附有「先前立場時間線」，新訊息與既有立場方向相反（打臉候選）也屬此類

寧缺勿濫：{max_n} 是上限不是目標，沒有夠格的議題就輸出空陣列 []。
【近日已挑過的議題】清單中的議題不要重複挑，除非出現「新的具體宣稱」（如官方首次回應、
新文件曝光、關鍵人物公開表態）——此時 claim 必須是那個新宣稱，並在 why 說明新在哪裡。

只輸出 JSON 陣列，不要多餘文字。每題格式：
{{"topic": "議題標題(≤30字)", "claim": "待驗證的具體宣稱(≤60字，一句可被推翻的話)",
 "entities": ["相關實體，優先用實體清單中的代號"],
 "scores": {{"relevance": 1-5, "verifiability": 1-5, "uncertainty": 1-5,
            "resonance": 1-5, "imbalance": 1-5}},
 "why": "為何值得深查(≤50字)", "directions": "建議查證方向(≤60字)"}}"""


def _entity_catalog() -> str:
    """給 LLM 的實體清單，標注持股/關注（relevance 評分的依據）。graph 缺失時回空字串。"""
    try:
        from graph.model import load_graph
        g = load_graph()
    except Exception:  # noqa: BLE001
        return ""
    lines = []
    for ticker, c in g.companies.items():
        tag = "持股" if g.status(ticker) == "hold" else "關注"
        lines.append(f"- {ticker}（{c.get('name', '')}）【{tag}】")
    themes = [st["name"] for t in g.themes for st in t.get("subthemes", [])]
    return "【實體清單】\n" + "\n".join(lines) + ("\n主題：" + "、".join(themes) if themes else "")


def _recent_picks(recent_digests: list[dict]) -> list[dict]:
    """收集近期 digest 已挑過的候選題（供 prompt 去重與程式端相似度過濾）。"""
    picks = []
    for d in recent_digests:
        for c in d.get("deepdive_candidates", []):
            if c.get("claim"):
                picks.append({"date": str(d.get("generated_at", ""))[:10],
                              "topic": c.get("topic", ""), "claim": c["claim"]})
    return picks


def _bigrams(s: str) -> set[str]:
    s = "".join(str(s).split())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _similar(a: str, b: str) -> float:
    """字元 bigram Jaccard 相似度（中文無需斷詞，足以擋住近乎同句的重複 claim）。"""
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def _validate(raw: list, past_claims: list[str]) -> list[dict]:
    """程式端硬性過濾：claim 必填、分數 clamp、總分門檻、近日重複剔除、取前 N。"""
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim", "")).strip()
        if not claim:  # 寫不出具體宣稱的候選直接丟棄（擋觀點型水題）
            continue
        scores = item.get("scores") or {}
        clamped = {}
        for k in SCORE_KEYS:
            try:
                clamped[k] = max(1, min(5, int(scores.get(k, 1))))
            except (TypeError, ValueError):
                clamped[k] = 1
        total = sum(clamped.values())
        if total < MIN_TOTAL:
            continue
        if any(_similar(claim, pc) >= SIM_THRESHOLD for pc in past_claims):
            logger.info("挑題去重：略過近日已挑過的相似議題「%s」", item.get("topic", claim))
            continue
        out.append({
            "topic": str(item.get("topic", "")).strip()[:40] or claim[:40],
            "claim": claim[:80],
            "entities": [str(e).strip() for e in (item.get("entities") or []) if str(e).strip()][:6],
            "scores": clamped,
            "total": total,
            "why": str(item.get("why", "")).strip()[:70],
            "directions": str(item.get("directions", "")).strip()[:80],
        })
    out.sort(key=lambda c: c["total"], reverse=True)
    return out[:MAX_CANDIDATES]


def pick_topics(entry: dict, recent_digests: list[dict], model: str, api_key: str) -> list[dict]:
    """從一份 digest entry 挑出 0~MAX_CANDIDATES 個深查候選題。失敗回空清單（呼叫端不受影響）。"""
    from .memory_extract import _digest_text, _parse_json_array
    from . import memory_link, summarizer

    body = _digest_text(entry)
    if not body.strip():
        return []

    context_parts = [body]
    catalog = _entity_catalog()
    if catalog:
        context_parts.append(catalog)
    # 立場時間線：讓 imbalance 維度能偵測「新訊息與既有立場矛盾」的打臉候選
    timeline = memory_link.build_timeline([{"text": body}], recent_digests)
    if timeline:
        context_parts.append(timeline)
    picks = _recent_picks(recent_digests)
    if picks:
        lines = [f"- {p['date']} {p['topic']}｜{p['claim']}" for p in picks[-15:]]
        context_parts.append("【近日已挑過的議題（勿重複，除非有新的具體宣稱）】\n" + "\n".join(lines))

    data = summarizer._post_chat(api_key, {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT.format(max_n=MAX_CANDIDATES)},
            {"role": "user", "content": "\n\n".join(context_parts)},
        ],
    })
    raw = _parse_json_array(data["choices"][0]["message"]["content"])
    candidates = _validate(raw, [p["claim"] for p in picks])
    logger.info("深查挑題：模型提出 %d 題 → 過濾後 %d 題", len(raw), len(candidates))
    return candidates
