from __future__ import annotations

import json
import re

from reports import llm  # 重用既有的 OpenRouter 客戶端與金鑰載入
from .model import Graph

STRONG_MODEL = "anthropic/claude-opus-4.8"  # 判斷用強模型

SUGGEST_SYSTEM = (
    "你是資深產業供應鏈分析師。我會給你一家公司，請草擬它在供應鏈的關係，供人工審核（不是最終定論）。\n"
    "輸出單一 JSON 物件：{\"role\":..., \"upstream\":[...], \"downstream\":[...], "
    "\"competitors\":[...], \"themes\":[...], \"notes\":[...]}。\n"
    "- upstream=主要供應商、downstream=主要客戶、competitors=主要競爭對手，各列 3~8 個最重要的。\n"
    "- 每個實體用其通用代號（美股 ticker、台股代號），無公開代號用中文簡稱。\n"
    "- 若『已追蹤清單』中有對應公司，優先使用清單裡的代號。\n"
    "- themes 用「主題/子題」字串描述它所屬領域（例如「光通訊/CPO」）。\n"
    "- notes 放 2~4 條人工審核時該注意的重點（如產品占比、關鍵變數、不確定處）。\n"
    "務實、不杜撰；不確定的在 notes 標明。只輸出 JSON。"
)


def _parse(text: str) -> dict:
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", t, re.S)
    return json.loads(m.group(0) if m else t)


def suggest(ticker: str, graph: Graph, api_key: str) -> tuple[dict, float]:
    c = graph.company(ticker) or {}
    tracked = "、".join(graph.companies)
    user = (
        f"公司：{ticker} {c.get('name', '')}（目前註記角色：{c.get('role', '(未填)')}）\n"
        f"已追蹤清單（有對應請優先用這些代號）：{tracked}\n\n"
        f"請草擬 {ticker} 的供應鏈關係。"
    )
    res = llm.chat(STRONG_MODEL, SUGGEST_SYSTEM, user, api_key)
    return _parse(res["text"]), res.get("cost") or 0


def to_yaml_block(ticker: str, name: str, draft: dict) -> str:
    """輸出可貼進 graph.yaml 的片段（供人工審核後貼上）。"""
    def arr(xs):
        return "[" + ", ".join(str(x) for x in (xs or [])) + "]"
    return (
        f"  {ticker}:\n"
        f"    name: {name}\n"
        f"    role: {draft.get('role', '')}\n"
        f"    upstream: {arr(draft.get('upstream'))}\n"
        f"    downstream: {arr(draft.get('downstream'))}\n"
        f"    competitors: {arr(draft.get('competitors'))}"
    )
