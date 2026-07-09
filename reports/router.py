from __future__ import annotations

import json
import logging
import re

from .config import ReportsConfig
from . import llm

logger = logging.getLogger(__name__)

ROUTER_SYSTEM = (
    "你是資深台股財報分析師。我會給你一家公司的基本資料與其財報「附註目錄」。"
    "請判斷：對『這家公司、這個產業』的投資分析而言，哪些附註最值得深入擷取，以及每一節要特別看什麼。\n"
    "規則：\n"
    "1. 挑 4~8 個最有投資價值的附註，排除純例行/樣板（如編製依據、準則適用宣告）。\n"
    "2. 依此產業特性給重點，例如：半導體看分部/製程別營收、產能利用、資本支出、存貨；"
    "IC 設計/EMS 看客戶集中度、存貨與毛利結構；金融看放款結構、逾放、利差、資本適足。\n"
    "3. 每個附註標 mode：以數字表格為主用 table，以文字敘述為主用 narrative。\n"
    "4. note_title 需對應目錄中實際存在的節（用其標題或關鍵字，能讓系統定位到那一節）。\n"
    "只輸出 JSON 陣列，每個元素為 "
    '{"note_title": "...", "focus": "要擷取的重點", "mode": "table" 或 "narrative"}，不要任何多餘文字。'
)


def _parse_json(text: str) -> list[dict]:
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\[.*\]", t, re.S)
    if m:
        t = m.group(0)
    return json.loads(t)


def plan_extraction(cfg: ReportsConfig, stock: str, name: str, industry: str,
                    notes: list[dict], api_key: str) -> tuple[list[dict], float]:
    """用強模型讀『目錄+產業』，產出這家公司專屬的擷取計畫。"""
    toc = "\n".join(f"- {n['title']}（印刷頁 {n['start']}–{n['end']}）" for n in notes)
    user = f"公司：{stock} {name}（產業：{industry}）\n\n財報附註目錄：\n{toc}"
    res = llm.chat(cfg.strong_model, ROUTER_SYSTEM, user, api_key)
    try:
        plan = _parse_json(res["text"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("路由計畫 JSON 解析失敗，將回退固定主題：%s", exc)
        plan = []
    return plan, res.get("cost") or 0
