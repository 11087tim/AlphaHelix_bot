"""用 Sonnet 把一份已彙整的 digest 萃取成結構化立場紀錄（Phase 2 記憶帳本的產生層）。

立場評分是趨勢偵測的核心、會複利放大誤差，故用較穩的模型；實體對齊在程式端用 graph 別名做，
LLM 只負責 stance(-2..+2)＋一句話＋關鍵驅動。
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

_STANCE_RUBRIC = (
    "stance 立場分數（對該實體的投資展望）：+2 強烈偏多/重大利多；+1 偏多/正面；"
    "0 中性、混合或僅陳述；-1 偏空/負面；-2 強烈偏空/重大利空。"
)


def _entity_catalog() -> tuple[str, dict]:
    """回傳 (給 LLM 的實體清單文字, 名稱→(canonical_key, kind) 對照)。"""
    try:
        from graph.model import load_graph
        g = load_graph()
    except Exception:  # noqa: BLE001
        return "", {}
    lines, lookup = [], {}
    for ticker, c in g.companies.items():
        name = c.get("name", "")
        lines.append(f"- {ticker}（{name}）")
        lookup[ticker.lower()] = (ticker, "ticker")
        for a in [name] + (c.get("aka") or []):
            if str(a).strip():
                lookup[str(a).strip().lower()] = (ticker, "ticker")
    themes = []
    for t in g.themes:
        for st in t.get("subthemes", []):
            label = f"{t['name']}/{st['name']}"
            themes.append(st["name"])
            lookup[st["name"].strip().lower()] = (label, "theme")
    cat = "公司：\n" + "\n".join(lines) + "\n主題：" + "、".join(themes)
    return cat, lookup


def _digest_text(entry: dict) -> str:
    parts = []
    for sec in (entry.get("account_sections") or []) + (entry.get("keyword_sections") or []):
        s = sec.get("summary", "").strip()
        if s:
            parts.append(s)
    return "\n\n".join(parts)


def _parse_json_array(text: str) -> list:
    """從模型輸出抽出 JSON 陣列，容忍 ```json 圍欄與前後雜訊。"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        m = re.search(r"(\[.*\])", text, re.S)
        if m:
            text = m.group(1)
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def extract_records(entry: dict, model: str, api_key: str) -> list[dict]:
    """把一份 digest entry 萃取成立場紀錄清單。"""
    body = _digest_text(entry)
    if not body.strip():
        return []
    catalog, lookup = _entity_catalog()

    system = (
        "你是財經資訊萃取器。讀入一份已彙整的投資觀點 digest，找出其中對『特定實體』（公司或主題）"
        "明確表達了投資立場/看法者，輸出 JSON 陣列。" + _STANCE_RUBRIC + "\n"
        "只收錄有實際立場或方向性判斷的實體；純事實陳述、沒有觀點的略過。\n"
        "entity 欄位：若屬於下列清單，用清單中的代號或主題名；若是清單外但可明確辨識的上市公司，"
        "用其股票代號（如 ORCL、CBRS）；其餘用簡短名稱。\n"
        "每筆：{\"entity\":..., \"stance\": 整數 -2~2, \"claim\": 一句話立場(≤40字), "
        "\"drivers\": 關鍵驅動(≤25字)}。只輸出 JSON 陣列，不要多餘文字。\n"
        f"【實體清單】\n{catalog}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": body},
        ],
    }
    try:
        from . import summarizer
        data = summarizer._post_chat(api_key, payload)
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("記憶萃取呼叫失敗，略過本次：%s", exc)
        return []

    raw = _parse_json_array(content)
    date = str(entry.get("generated_at", ""))[:10]
    records: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ent = str(item.get("entity", "")).strip()
        if not ent:
            continue
        try:
            stance = int(item.get("stance", 0))
        except (TypeError, ValueError):
            stance = 0
        stance = max(-2, min(2, stance))
        canonical, kind = lookup.get(ent.lower(), (ent, "other"))
        records.append({
            "date": date,
            "generated_at": entry.get("generated_at", ""),
            "digest_id": entry.get("id", ""),
            "entity": canonical,
            "kind": kind,
            "stance": stance,
            "claim": str(item.get("claim", "")).strip()[:60],
            "drivers": str(item.get("drivers", "")).strip()[:40],
        })
    logger.info("記憶萃取：digest %s → %d 筆立場紀錄", entry.get("id", ""), len(records))
    return records
