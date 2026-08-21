"""深查引擎（跨源印證 v2）：對挑題模組選出的議題做深入查證。

走 OpenRouter + web plugin（engine=native）：Anthropic 模型會使用原生 server-side
web search（搜尋迴圈在提供商端自動執行，計價直通），citations 以 annotations 回傳。
限制：OpenRouter 無 web_fetch（讀取網頁全文），溯源靠搜尋結果摘錄；方法論已要求
無法取得原文時誠實標注。

用法（ad-hoc）：python3 -m src.deepdive topics.json [out_dir]
  topics.json = 挑題模組輸出的候選題陣列（含 topic/claim/entities/why/directions）。
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

VERDICT_EMOJI = {"證實": "✅", "部分證實": "⚠️", "查證不支持": "❌", "證據不足": "❓"}
# 新格式：**裁定**：【部分證實】一句話…；舊格式（相容）：**裁定**：**部分證實**。一句話…
_VERDICT_NEW = re.compile(r"裁定[^【\n]*【(證實|部分證實|查證不支持|證據不足)】\s*[：:，,。]?\s*(.*)")
_VERDICT_OLD = re.compile(r"裁定\*{0,2}[：:]\s*\*\*([^*\n]+)\*\*[。．]?\s*(.*)")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-opus-5"
TIMEOUT = 600          # 深查含多輪伺服器端搜尋，單題可能跑數分鐘
MAX_RETRIES = 2

# 查證方法論：把人工深查流程固化成步驟。付費牆誠實原則寫死於此。
_METHODOLOGY = """你是投資情報系統的「深查員」。系統的挑題模組從每日內容選出一個重要但未證實的議題，
你的任務是用網路搜尋做深入查證，產出一份繁體中文深查報告。輸出必須基於事實、絕不捏造。

嚴格依照以下方法論執行：

1. 溯源：找出宣稱的原始出處（一手文件/官方聲明/原始研究報告 vs 分析師 note vs 媒體轉述）。
   注意：N 家媒體引用同一來源 ≠ N 個獨立來源。搜尋時優先鎖定原始出處（如 SEC 文件、
   官方新聞稿、原始報告），而非二手轉述。
2. 交叉：至少找兩個「獨立」來源比對；找不到就明說「單一來源」。留意台媒常互相改寫。
3. 分級：報告中每個關鍵宣稱標注【事實】（官方文件/多獨立來源證實）、【機構估計】（研究機構/
   分析師推估）或【傳聞】（單一來源、未證實），並附時效日期。
4. 框架化：把查證結果放進供應鏈/持股脈絡（文末附有實體清單），分析誰受益誰受害、
   市場解讀哪裡失真。有反方觀點（如機構認為過度反應）必須並陳。
5. 可驗證節點：列出未來哪個時點、哪個訊號能升級或推翻本結論（財報會、法說、官方文件、產能數據）。

誠實原則：
- 付費牆內容或只有摘錄、無法讀到原文的，標注「僅據轉述/摘錄，無法取得原文」，
  寧可降級結論也不腦補。
- 查證結果與原宣稱矛盾時直接說「查證不支持此宣稱」；證據不足就說不足。

報告格式（精簡有力，總長 800~1500 字）：
## 深查：{議題標題}
**裁定**：【證實】（或【部分證實】【查證不支持】【證據不足】四選一，方括號必須保留）
緊接一句話核心發現（≤60字，說明市場敘事哪裡失真或哪裡被證實）。裁定行獨立成段，
需要補充的裁定細節放下一段。
### 查證過程與證據
（溯源結果、關鍵證據列點、每點標【事實/機構估計/傳聞】與日期）
### 供應鏈與持股意涵
（框架化分析，含反方觀點）
### 可驗證節點
（列點：時點 × 訊號 × 會如何改變結論）
### 資料品質備註
（單一來源警語、付費牆限制、數據分歧等）"""


def _topic_prompt(topic: dict, graph_context: str | None) -> str:
    parts = [
        f"議題：{topic.get('topic', '')}",
        f"待驗證宣稱：{topic.get('claim', '')}",
        f"相關實體：{'、'.join(topic.get('entities', []))}",
        f"挑題理由：{topic.get('why', '')}",
        f"建議查證方向：{topic.get('directions', '')}",
        f"今天日期：{datetime.now().strftime('%Y-%m-%d')}（查證時注意資訊時效）",
    ]
    if graph_context:
        parts.append(f"\n{graph_context}")
    return "\n".join(parts)


def _strip_md(s: str) -> str:
    return re.sub(r"\*+|【|】", "", s).strip()


def parse_verdict(report: str) -> tuple[str, str]:
    """從報告抽出（裁定等級, 一句話 takeaway）。抽不出時回（"證據不足", 空字串）保守呈現。"""
    m = _VERDICT_NEW.search(report)
    if m:
        verdict, rest = m.group(1), m.group(2)
    else:
        m = _VERDICT_OLD.search(report)
        if not m:
            return "證據不足", ""
        raw, rest = _strip_md(m.group(1)), m.group(2)
        # 舊格式裁定詞可能帶說明（如「核心證實，細節須修正」「證實（位元出貨部分）」）：
        # 含否定詞優先，純「證實」開頭才算證實，其餘含「證實」者視為部分證實
        if "不支持" in raw:
            verdict = "查證不支持"
        elif "證據不足" in raw:
            verdict = "證據不足"
        elif raw.startswith("部分證實") or ("證實" in raw and not raw.startswith("證實")):
            verdict = "部分證實"
        elif raw.startswith("證實"):
            verdict = "證實"
        else:
            verdict = "證據不足"
    takeaway = _strip_md(rest.split("\n", 1)[0])
    # 取到第一個句號為止，過長截斷
    period = takeaway.find("。")
    if period > 0:
        takeaway = takeaway[:period]
    return verdict, takeaway[:90]


def _sources_from_annotations(message: dict) -> str:
    """把 OpenRouter 回傳的 url_citation annotations 整理成文末來源清單。"""
    seen: dict[str, str] = {}
    for a in message.get("annotations") or []:
        c = a.get("url_citation") or {}
        url = c.get("url", "")
        if url and url not in seen:
            seen[url] = c.get("title", "") or url
    if not seen:
        return ""
    return "\n\n**引用來源**\n" + "\n".join(f"- [{t}]({u})" for u, t in seen.items())


def investigate(topic: dict, api_key: str, graph_context: str | None = None,
                model: str = MODEL) -> dict:
    """深查一個議題，回傳 {topic, report, usage}。由呼叫端決定失敗處理。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _METHODOLOGY},
            {"role": "user", "content": _topic_prompt(topic, graph_context)},
        ],
        # engine=native：Anthropic 模型走原生 server-side web search（計價直通提供商）
        "plugins": [{"id": "web", "engine": "native"}],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                raise RuntimeError(str(data["error"]))
            break
        except (requests.exceptions.RequestException, RuntimeError) as exc:
            last_exc = exc
            logger.warning("深查呼叫失敗（第 %d/%d 次）：%s", attempt, MAX_RETRIES, exc)
            if attempt == MAX_RETRIES:
                raise
    message = data["choices"][0]["message"]
    content = (message.get("content") or "").strip()
    # 伺服器端搜尋輪之間的過場旁白會殘留在正文開頭，只保留報告本體
    idx = content.find("## 深查")
    if idx > 0:
        content = content[idx:]
    report = content + _sources_from_annotations(message)
    verdict, takeaway = parse_verdict(report)
    return {"topic": topic, "report": report, "verdict": verdict, "takeaway": takeaway,
            "usage": data.get("usage", {})}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import os
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("缺少 OPENROUTER_API_KEY", file=sys.stderr)
        return 1
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 1
    topics = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("deepdive_out")
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        if __package__:
            from .graph_link import load_graph_context
        else:
            from src.graph_link import load_graph_context
        graph_context = load_graph_context()
    except Exception:  # noqa: BLE001
        graph_context = None

    for i, topic in enumerate(topics, 1):
        logger.info("[%d/%d] 深查：%s", i, len(topics), topic.get("topic", ""))
        try:
            result = investigate(topic, api_key, graph_context)
        except Exception as exc:  # noqa: BLE001
            logger.error("深查失敗，跳過：%s", exc)
            continue
        path = out_dir / f"deepdive_{datetime.now().strftime('%Y%m%d')}_{i}.md"
        path.write_text(result["report"], encoding="utf-8")
        logger.info("完成 → %s（usage: %s）", path, result["usage"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
