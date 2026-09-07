"""深查引擎（跨源印證 v2）：對挑題模組選出的議題做深入查證。

走 Claude Code CLI headless 模式（claude -p）＋ Claude 訂閱 OAuth：用量計入訂閱額度，
不走 OpenRouter API 計費。認證來源（擇一）：本機 claude 登入，或環境變數
CLAUDE_CODE_OAUTH_TOKEN（在有訂閱登入的機器跑 `claude setup-token` 產生後貼到 .env）。
工具開放 WebSearch＋WebFetch：比舊 OpenRouter 路徑（只有 search）多了讀取網頁原文
的能力，溯源可直接取回一手文件；引用來源改由模型依方法論在文末自列。

用法（ad-hoc）：python3 -m src.deepdive topics.json [out_dir]
  topics.json = 挑題模組輸出的候選題陣列（含 topic/claim/entities/why/directions）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

VERDICT_EMOJI = {"證實": "✅", "部分證實": "⚠️", "查證不支持": "❌", "證據不足": "❓"}
# 新格式：**裁定**：【部分證實】一句話…；舊格式（相容）：**裁定**：**部分證實**。一句話…
_VERDICT_NEW = re.compile(r"裁定[^【\n]*【(證實|部分證實|查證不支持|證據不足)】\s*[：:，,。]?\s*(.*)")
_VERDICT_OLD = re.compile(r"裁定\*{0,2}[：:]\s*\*\*([^*\n]+)\*\*[。．]?\s*(.*)")

MODEL = os.environ.get("DEEPDIVE_CLAUDE_MODEL", "claude-opus-5")
TIMEOUT = 600          # 深查含多輪搜尋/讀原文，單題可能跑數分鐘
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

工具使用：用 WebSearch 找來源、WebFetch 讀取網頁原文；溯源時盡量 fetch 一手文件全文，
而非只靠搜尋結果摘錄。除了網路搜尋/讀網頁外不要使用其他工具。

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
（單一來源警語、付費牆限制、數據分歧等）

**引用來源**
（列點：- [來源標題](URL)，只列實際引用過的網頁，勿捏造連結）"""


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


def clean_report(content: str) -> str:
    """去掉伺服器端搜尋輪之間殘留在正文開頭的過場旁白，只留報告本體。"""
    idx = content.find("## 深查")
    return content[idx:] if idx > 0 else content


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
    # 取到第一個句號為止；沒有句號就在 90 字內找最後一個分句符號收尾，避免截在半句
    period = takeaway.find("。")
    if period > 0:
        takeaway = takeaway[:period]
    if len(takeaway) > 90:
        cut = max(takeaway.rfind(p, 0, 90) for p in "；）」，")
        takeaway = takeaway[:cut + 1] if cut > 30 else takeaway[:90]
    return verdict, takeaway


def find_claude_bin() -> str:
    """找 claude CLI 執行檔：CLAUDE_BIN 環境變數優先，其次 PATH。找不到即報錯。"""
    bin_path = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not bin_path:
        raise RuntimeError(
            "找不到 claude CLI。請先安裝（npm install -g @anthropic-ai/claude-code），"
            "並以訂閱帳號認證：本機 `claude` 登入，或在 .env 設 CLAUDE_CODE_OAUTH_TOKEN"
            "（在已登入的機器跑 `claude setup-token` 產生）；"
            "CLI 不在 PATH 時以 CLAUDE_BIN 指定路徑。")
    return bin_path


def investigate(topic: dict, graph_context: str | None = None,
                model: str = MODEL) -> dict:
    """深查一個議題，回傳 {topic, report, usage}。由呼叫端決定失敗處理。

    走 claude -p headless（訂閱 OAuth）：只開放 WebSearch/WebFetch，
    輸出取 --output-format json 的 result 欄位。
    """
    cmd = [
        find_claude_bin(), "-p", _topic_prompt(topic, graph_context),
        "--model", model,
        "--append-system-prompt", _METHODOLOGY,
        "--allowedTools", "WebSearch,WebFetch",
        "--output-format", "json",
    ]
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
            if proc.returncode != 0:
                raise RuntimeError(f"claude CLI 退出碼 {proc.returncode}："
                                   f"{(proc.stderr or proc.stdout).strip()[:500]}")
            data = json.loads(proc.stdout)
            if data.get("is_error"):
                raise RuntimeError(f"claude CLI 回報錯誤（{data.get('subtype')}）："
                                   f"{str(data.get('result', ''))[:500]}")
            break
        except (subprocess.SubprocessError, OSError, RuntimeError,
                json.JSONDecodeError) as exc:
            last_exc = exc
            logger.warning("深查呼叫失敗（第 %d/%d 次）：%s", attempt, MAX_RETRIES, exc)
            if attempt == MAX_RETRIES:
                raise
    report = clean_report((data.get("result") or "").strip())
    verdict, takeaway = parse_verdict(report)
    usage = dict(data.get("usage") or {})
    if data.get("total_cost_usd") is not None:
        usage["total_cost_usd"] = data["total_cost_usd"]
    return {"topic": topic, "report": report, "verdict": verdict, "takeaway": takeaway,
            "usage": usage}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")  # 載入 CLAUDE_CODE_OAUTH_TOKEN 等
    try:
        find_claude_bin()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
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
            result = investigate(topic, graph_context)
        except Exception as exc:  # noqa: BLE001
            logger.error("深查失敗，跳過：%s", exc)
            continue
        path = out_dir / f"deepdive_{datetime.now().strftime('%Y%m%d')}_{i}.md"
        path.write_text(result["report"], encoding="utf-8")
        logger.info("完成 → %s（usage: %s）", path, result["usage"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
