"""跨時間記憶注入層：組成『先前觀點時間線』供合成時偵測趨勢/反轉/矛盾，於頂部產出「📈 本期變化」。

Phase 2：優先讀結構化立場帳本（memory_store），依本批推文提到的實體撈歷史立場軌跡（含數值，視野長）。
帳本尚無相關紀錄時，回退 Phase 1：即時解析最近幾份 digest 的 🤖 立場句。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

MAX_DIGESTS = 6           # Phase 1 回退：回看最近幾個時段
MAX_ITEMS_PER_DIGEST = 8
STANCE_MAXLEN = 130
MEMORY_DAYS = 45          # Phase 2：時間線回看天數
MAX_POINTS_PER_ENTITY = 8

_CITE_RE = re.compile(r"\[[^\]]*\]")


# ---------- Phase 2：結構化帳本 ----------

def _entities_in(tweets: list[dict]) -> tuple[set[str], dict]:
    """回傳 (本批提到的實體集合, ticker→顯示名)。實體含 graph ticker 與 主題/子題標籤。"""
    try:
        from graph.model import load_graph
        g = load_graph()
    except Exception:  # noqa: BLE001
        return set(), {}
    text = "\n".join(t.get("text", "") for t in tweets)
    tickers, themes = g.mentions(text)
    names = {tk: (g.company(tk) or {}).get("name", tk) for tk in tickers}
    return set(tickers) | set(themes), names


def _phase2_timeline(tweets: list[dict]) -> str | None:
    entities, names = _entities_in(tweets)
    if not entities:
        return None
    try:
        from .memory_store import MemoryStore
        records = MemoryStore().for_entities(entities, days=MEMORY_DAYS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("讀取記憶帳本失敗，回退 Phase 1：%s", exc)
        return None
    if not records:
        return None

    by_entity: dict[str, list[dict]] = {}
    for r in records:
        by_entity.setdefault(r["entity"], []).append(r)

    lines = []
    for entity, recs in by_entity.items():
        # 同一天多份 digest 只留最後一筆，讓軌跡呈現「每日」趨勢而非日內重複
        per_day = {str(r.get("date", "")): r for r in recs}
        recs = [per_day[d] for d in sorted(per_day)][-MAX_POINTS_PER_ENTITY:]
        disp = names.get(entity, entity)
        label = f"{disp}({entity})" if disp != entity else entity
        pts = []
        for r in recs:
            s = r.get("stance", 0)
            sign = f"+{s}" if s > 0 else str(s)
            claim = (r.get("claim") or "").strip()
            pts.append(f"{str(r.get('date',''))[5:]} {sign}｜{claim}" if claim
                       else f"{str(r.get('date',''))[5:]} {sign}")
        lines.append(f"- {label}：" + " → ".join(pts))

    header = ("【先前立場時間線（記憶帳本，各實體歷史立場軌跡；stance -2~+2；"
              "供偵測趨勢升溫/降溫與反轉，非本批新事實）】")
    logger.info("附上結構化立場時間線：%d 個實體", len(lines))
    return header + "\n" + "\n".join(lines)


# ---------- Phase 1：即時解析 digest（回退用）----------

def _pairs(summary: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    cur = ""
    for raw in summary.split("\n"):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#"):
            cur = s.lstrip("#").strip()
        elif s.startswith("**") and s.rstrip().endswith("**"):
            cur = s.strip("*").strip().rstrip("：:")
        elif "🤖" in s:
            stance = s.split("🤖", 1)[1]
            stance = re.sub(r"^\s*延伸推論[：:]\s*", "", stance)
            stance = _CITE_RE.sub("", stance).strip()
            if stance:
                out.append((cur, stance[:STANCE_MAXLEN]))
    return out


def _phase1_timeline(recent_digests: list[dict], k: int = MAX_DIGESTS) -> str | None:
    picked = list(recent_digests)[:k]
    picked.reverse()
    blocks: list[str] = []
    for d in picked:
        items: list[tuple[str, str]] = []
        for sec in (d.get("account_sections") or []) + (d.get("keyword_sections") or []):
            items.extend(_pairs(sec.get("summary", "")))
        if not items:
            continue
        lines = [f"[{d.get('generated_at', '')}]"]
        for head, stance in items[:MAX_ITEMS_PER_DIGEST]:
            lines.append(f"  - {head}｜先前觀點：{stance}" if head else f"  - {stance}")
        blocks.append("\n".join(lines))
    if not blocks:
        return None
    header = ("【先前時段觀點時間線（你在前幾個時段對各主題的判斷，供偵測『變化』用；"
              "這不是本批推文的事實，切勿當成新消息複述）】")
    logger.info("附上先前觀點時間線（Phase 1 回退）：%d 個時段", len(blocks))
    return header + "\n" + "\n".join(blocks)


def build_timeline(tweets: list[dict], recent_digests: list[dict]) -> str | None:
    """優先用結構化帳本；帳本對本批實體無紀錄時回退即時解析。"""
    return _phase2_timeline(tweets) or _phase1_timeline(recent_digests)
