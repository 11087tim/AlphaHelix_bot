"""跨時間記憶（Phase 1，即時解析）：把最近幾份 digest 濃縮成『先前觀點時間線』，
注入下一次合成，讓 Opus 能對照今日與先前，於頂部產出「📈 本期變化」（趨勢/反轉/矛盾）。

不建新 store，直接讀 digests.json 既有內容——只抽各子主題的標題與 🤖 延伸推論（立場句）。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

MAX_DIGESTS = 6           # 回看最近幾個時段
MAX_ITEMS_PER_DIGEST = 8  # 每個時段最多取幾條立場，控 token
STANCE_MAXLEN = 130       # 每條立場句截斷長度（僅供模型比對，非顯示）

_CITE_RE = re.compile(r"\[[^\]]*\]")


def _pairs(summary: str) -> list[tuple[str, str]]:
    """從一份 digest 的 summary markdown，抽出 (子主題標題, 🤖 立場句) 配對。"""
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
            stance = _CITE_RE.sub("", stance).strip()  # 去掉 [n]/[附圖N] 引用雜訊
            if stance:
                out.append((cur, stance[:STANCE_MAXLEN]))
    return out


def build_timeline(recent_digests: list[dict], k: int = MAX_DIGESTS) -> str | None:
    """recent_digests 由新到舊（DigestStore.recent 的輸出）。回傳時間線文字（無則 None）。"""
    picked = list(recent_digests)[:k]
    picked.reverse()  # 轉成由舊到新，時間線好讀

    blocks: list[str] = []
    for d in picked:
        date = d.get("generated_at", "")
        items: list[tuple[str, str]] = []
        for sec in (d.get("account_sections") or []) + (d.get("keyword_sections") or []):
            items.extend(_pairs(sec.get("summary", "")))
        if not items:
            continue
        lines = [f"[{date}]"]
        for head, stance in items[:MAX_ITEMS_PER_DIGEST]:
            lines.append(f"  - {head}｜先前觀點：{stance}" if head else f"  - {stance}")
        blocks.append("\n".join(lines))

    if not blocks:
        return None
    header = ("【先前時段觀點時間線（你在前幾個時段對各主題的判斷，供偵測『變化』用；"
              "這不是本批推文的事實，切勿當成新消息複述）】")
    logger.info("附上先前觀點時間線：%d 個時段", len(blocks))
    return header + "\n" + "\n".join(blocks)
