"""重大訊息判讀層：追蹤股的新重訊 → LLM 分類對 EPS 預估的影響。

    python -m reports.main events [回看天數，預設 7]

流程：data/announcements/*.jsonl（VM cron 收的重訊庫）→ 過濾 config 追蹤股 →
Haiku 逐則判讀（material？影響營收/毛利/一次性？方向？影響哪季？）→
結果快取 data/announcements/interpreted.jsonl（同 key 不重判）→
material 事件彙整成 analysis/events_latest.md，供對照 nowcast 調整。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

from .config import ReportsConfig
from . import llm
from .announcements import STORE_DIR

logger = logging.getLogger(__name__)

INTERPRETED = STORE_DIR / "interpreted.jsonl"

CLASSIFY_SYSTEM = (
    "你是台股分析師。以下是一則上市櫃公司重大訊息公告。請判斷它對該公司「未來幾季 EPS 預估」的影響，"
    "只輸出一個 JSON 物件：\n"
    '{"material": true/false（會不會改變營收或獲利預估；例行公告如股東會、除權息基準日、'
    "背書保證額度例行調整、發言人異動等為 false），\n"
    '"impact_type": "營收"|"毛利率"|"一次性損益"|"股本"|"財務結構"|"無",\n'
    '"direction": "正面"|"負面"|"中性"|"不明",\n'
    '"quarters": 受影響季度清單，如 ["2026Q3","2026Q4"]（依事實發生日與內容推斷；不明給 []），\n'
    '"magnitude": "高"|"中"|"低"（相對公司規模的影響量級），\n'
    '"summary": "一句話講清楚發生什麼事與為何影響/不影響預估（繁體中文，<50字）"}\n'
    "只根據公告內容判斷，不要杜撰。金額若有揭露，量級判斷要用它。"
)


def _load_recent(days: int) -> list[dict]:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = []
    for p in sorted(STORE_DIR.glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("date", "") >= cutoff:
                rows.append(r)
    return rows


def _load_interpreted() -> dict[str, dict]:
    out = {}
    if INTERPRETED.exists():
        for line in INTERPRETED.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                out[r["key"]] = r
            except (json.JSONDecodeError, KeyError):
                continue
    return out


def run_events(cfg: ReportsConfig, days: int = 7) -> int:
    watch = set(cfg.stocks)
    recent = [r for r in _load_recent(days) if r["stock_id"] in watch]
    if not recent:
        logger.info("近 %d 天追蹤股（%d 檔）無重訊。重訊庫共 %d 則。",
                    days, len(watch), sum(1 for _ in _load_recent(days)))
        return 0
    seen = _load_interpreted()
    todo = [r for r in recent if r["key"] not in seen]
    logger.info("近 %d 天追蹤股重訊 %d 則（新 %d、已判讀 %d）", days, len(recent), len(todo), len(recent) - len(todo))

    api_key = llm.get_api_key()
    for r in todo:
        text = (f"公司：{r['stock_id']} {r['company']}（{r['market']}）\n"
                f"發言日期：{r['date']} {r['time']}，事實發生日：{r['fact_date']}，條款：{r['clause']}\n"
                f"主旨：{r['subject']}\n說明：\n{r['description'][:3000]}")
        res = llm.chat(cfg.cheap_model, CLASSIFY_SYSTEM, text, api_key)
        try:
            import re
            j = json.loads(re.search(r"\{.*\}", res["text"], re.S).group(0))
        except (AttributeError, json.JSONDecodeError):
            logger.warning("  %s %s 判讀解析失敗，跳過", r["stock_id"], r["subject"][:30])
            continue
        rec = {"key": r["key"], "stock_id": r["stock_id"], "company": r["company"],
               "date": r["date"], "subject": r["subject"], **j}
        seen[r["key"]] = rec
        with INTERPRETED.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        flag = "⚡" if j.get("material") else "·"
        logger.info("  %s %s %s → %s/%s/%s %s", flag, r["stock_id"], r["subject"][:36],
                    j.get("impact_type"), j.get("direction"), j.get("magnitude"), j.get("summary", "")[:40])

    # 彙整 material 事件
    hits = [seen[r["key"]] for r in recent if r["key"] in seen and seen[r["key"]].get("material")]
    lines = [f"# 追蹤股重訊判讀（近 {days} 天，至 {date.today().isoformat()}）", ""]
    if hits:
        lines += ["| 日期 | 股號 | 事件 | 影響 | 方向 | 量級 | 季度 |", "|---|---|---|---|---|---|---|"]
        for h in sorted(hits, key=lambda x: x["date"], reverse=True):
            lines.append(f"| {h['date']} | {h['stock_id']} {h['company']} | {h['summary']} "
                         f"| {h['impact_type']} | {h['direction']} | {h['magnitude']} "
                         f"| {'、'.join(h.get('quarters') or ['—'])} |")
        lines += ["", "→ 對照 analysis/<股號>_nowcast.md 手動調整，或等自動調整層。"]
    else:
        lines.append(f"近 {days} 天追蹤股共 {len(recent)} 則重訊，**均判定為例行公告（non-material）**。")
    out = cfg.data_dir / "analysis" / "events_latest.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    logger.info("完成：material %d／共 %d 則，彙整見 %s", len(hits), len(recent), out)
    return 0
