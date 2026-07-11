"""把 reports/ 產出的台股財報 brief，濃縮成『財報事實卡』注入 X 觀點合成，
讓 Opus 能把即時的 X 質化說法與過去季度的財報數字對照（印證／打臉／補盲點）。

只針對 graph.yaml 中設有 report_code 的公司，且推文實際提到者，才附上卡片，控制 token 與雜訊。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = PROJECT_ROOT / "reports_data" / "analysis"

_QUARTER_RE = re.compile(r"_(\d{4}Q[1-4])_zh\.md$")
_DETAIL_MARKER = "# 各附註擷取明細"
# 「一句話」總結有兩種格式：**一句話：整句**（整句粗體）或 **一句話：** 整句（僅標籤粗體）
_ONE_LINER_RE = re.compile(r"一句話[^：:]*[：:]\s*(.+)")
_BOLD_LEAD_RE = re.compile(r"\*\*(.+?)\*\*")


def _latest_brief(code: str) -> tuple[str, Path] | None:
    """找某台股代號最新一季的 brief（排除 aggregate 彙總檔）。回傳 (季別, 路徑)。"""
    if not ANALYSIS_DIR.exists():
        return None
    best: tuple[str, Path] | None = None
    for p in ANALYSIS_DIR.glob(f"{code}_*_zh.md"):
        if "aggregate" in p.name:
            continue
        m = _QUARTER_RE.search(p.name)
        if not m:
            continue
        q = m.group(1)
        if best is None or q > best[0]:  # 季別字串可直接比大小（YYYYQn）
            best = (q, p)
    return best


def _section(body: str, prefix: str) -> str:
    """取 `## <prefix>…` 到下一個 `## ` 之間的內容。"""
    lines = body.split("\n")
    out: list[str] = []
    collecting = False
    for ln in lines:
        if ln.startswith("## "):
            if collecting:
                break
            collecting = ln.startswith(f"## {prefix}")
            continue
        if collecting:
            out.append(ln)
    return "\n".join(out)


def _headlines(section_text: str, only_flags: bool = False, limit: int = 3) -> list[str]:
    """從條列抽出精簡標題：優先取 **粗體前導**，否則取整行去符號後前 40 字。"""
    out: list[str] = []
    for ln in section_text.split("\n"):
        s = ln.strip()
        if not s.startswith("-"):
            continue
        s = s.lstrip("-").strip()
        if only_flags and "🚩" not in s:
            continue
        s = s.replace("🚩", "").replace("⚠️", "").replace("✓", "").strip()
        bold = _BOLD_LEAD_RE.search(s)
        head = bold.group(1) if bold else s
        head = head.split("：")[0].split(":")[0].strip()
        if head:
            out.append(head[:40])
        if len(out) >= limit:
            break
    return out


def _one_liner(body: str) -> str:
    for ln in body.split("\n"):
        if "一句話" in ln:
            m = _ONE_LINER_RE.search(ln)
            if m:
                return re.sub(r"\s+", " ", m.group(1).strip().strip("*").strip())
    return ""


def _card(ticker: str, name: str, code: str, quarter: str, path: Path) -> str | None:
    body = path.read_text(encoding="utf-8").split(_DETAIL_MARKER)[0]
    one_liner = _one_liner(body)
    points = _headlines(_section(body, "一、"))
    flags = _headlines(_section(body, "三、"), only_flags=True)
    if not (one_liner or points or flags):
        return None
    tag = code if str(ticker) == str(code) else f"{ticker}／{code}"  # 台股代號本身即 ticker 時不重複
    parts = [f"● {name}（{tag}）— 依 {quarter} 財報（過去季度，非最新報價/近況）："]
    if one_liner:
        parts.append(f"  一句話：{one_liner}")
    if points:
        parts.append(f"  財報重點：{'；'.join(points)}")
    if flags:
        parts.append(f"  🚩紅旗：{'；'.join(flags)}")
    return "\n".join(parts)


def load_report_cards(tweets: list[dict]) -> str | None:
    """對這批推文，回傳其中提到、且有財報 brief 的公司事實卡（無則回 None）。"""
    try:
        from graph.model import load_graph
        graph = load_graph()
    except Exception as exc:  # noqa: BLE001
        logger.warning("載入 graph 失敗，略過財報事實卡：%s", exc)
        return None

    text = "\n".join(t.get("text", "") for t in tweets)
    try:
        tickers, _themes = graph.mentions(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("比對推文提及公司失敗，略過財報事實卡：%s", exc)
        return None

    cards: list[str] = []
    for ticker in tickers:
        c = graph.company(ticker) or {}
        code = c.get("report_code")
        if not code:
            continue
        latest = _latest_brief(str(code))
        if not latest:
            continue
        quarter, path = latest
        card = _card(ticker, c.get("name", ticker), str(code), quarter, path)
        if card:
            cards.append(card)

    if not cards:
        return None
    header = ("【財報事實卡（來自 reports/ 的過去季度財報，供與『即時』X 說法對照。"
              "引用數字務必註明季別，且明確區分：財報＝過去季度、X＝即時，勿把舊數字當成最新）】")
    logger.info("附上 %d 張財報事實卡：%s", len(cards), "、".join(
        re.findall(r"● ([^（]+)", "\n".join(cards))))
    return header + "\n" + "\n".join(cards)
