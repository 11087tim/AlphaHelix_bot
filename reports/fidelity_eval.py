from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import ReportsConfig
from . import llm
from .storage import ReportStorage

logger = logging.getLogger(__name__)

EXTRACT_SYSTEM = (
    "你是財報重點擷取助手。從以下財報片段，逐項擷取所有重要的財務資訊："
    "具體金額數字、年增減率、會計項目、重大事項、附註要點。"
    "務必保留原文的具體數字，不要遺漏、不要壓縮成籠統摘要、不要自行加以詮釋。用條列輸出。"
)

JUDGE_SYSTEM = (
    "你是嚴謹的財報稽核員。我會給你一段財報原文，以及某模型從這段擷取出的重點清單。"
    "請比對，列出【原文中存在、但擷取清單遺漏或數字寫錯】的『重要』財務資訊"
    "（具體金額、會計項目、重大事項、附註要點；忽略純版面/頁碼/無意義字串）。"
    "只列實質且重要的遺漏或錯誤，逐項簡短。若沒有重大遺漏，只回覆「無重大遺漏」。"
    "最後務必單獨一行用「遺漏數：N」標明你列出的重大遺漏項數（N 為整數）。"
)


def _chunks(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


def _sample_evenly(items: list, k: int) -> list[tuple[int, object]]:
    if k >= len(items):
        return list(enumerate(items))
    step = len(items) / k
    idxs = sorted({int(i * step) for i in range(k)})
    return [(i, items[i]) for i in idxs]


def _omission_count(judge_text: str) -> int:
    m = re.search(r"遺漏數[：:]\s*(\d+)", judge_text)
    if m:
        return int(m.group(1))
    return 0 if "無重大遺漏" in judge_text else -1  # -1 = 無法解析


def run_eval(cfg: ReportsConfig, stock: str, year: int, quarter: int, report_type: str = "consolidated") -> int:
    storage = ReportStorage(cfg.data_dir)
    txt_path = storage.text_dir / stock / f"{year}Q{quarter}_{report_type}_{cfg.language}.txt"
    if not txt_path.exists():
        logger.error("找不到文字檔：%s（請先 fetch + extract）", txt_path)
        return 1

    text = txt_path.read_text(encoding="utf-8")
    all_chunks = _chunks(text, cfg.chunk_chars)
    sample = _sample_evenly(all_chunks, cfg.eval_sample_chunks)
    api_key = llm.get_api_key()

    logger.info("評測 %s %dQ%d：全文 %d 字、%d 段，抽樣 %d 段（cheap=%s, judge=%s）",
                stock, year, quarter, len(text), len(all_chunks), len(sample),
                cfg.cheap_model, cfg.strong_model)

    lines = [f"# 保真度驗證：{stock} {year}Q{quarter}（{report_type}/{cfg.language}）",
             f"- 全文 {len(text):,} 字，切成 {len(all_chunks)} 段，抽樣 {len(sample)} 段",
             f"- 便宜模型（擷取）：`{cfg.cheap_model}`　強模型（裁決）：`{cfg.strong_model}`", ""]
    total_omissions = 0
    total_cost = 0.0
    unparsed = 0

    for n, (idx, chunk) in enumerate(sample, start=1):
        extract = llm.chat(cfg.cheap_model, EXTRACT_SYSTEM, f"財報片段：\n\n{chunk}", api_key)
        judge = llm.chat(cfg.strong_model, JUDGE_SYSTEM,
                         f"【原文】\n{chunk}\n\n【擷取清單】\n{extract['text']}", api_key)
        cost = (extract.get("cost") or 0) + (judge.get("cost") or 0)
        total_cost += cost
        miss = _omission_count(judge["text"])
        if miss < 0:
            unparsed += 1
            miss_disp = "無法解析"
        else:
            total_omissions += miss
            miss_disp = str(miss)
        logger.info("  段 %d/%d（原文第 %d 段）：遺漏 %s，本段成本 $%.4f",
                    n, len(sample), idx, miss_disp, cost)
        lines += [f"## 抽樣段 {n}（原文第 {idx} 段）",
                  f"**Opus 裁決遺漏數：{miss_disp}**", "",
                  "<details><summary>便宜模型擷取結果</summary>\n\n" + extract["text"] + "\n\n</details>", "",
                  "<details><summary>Opus 裁決（遺漏/錯誤）</summary>\n\n" + judge["text"] + "\n\n</details>", ""]

    avg = total_omissions / max(1, len(sample) - unparsed)
    summary = (f"抽樣 {len(sample)} 段，Opus 判定總遺漏 {total_omissions} 項"
               f"（平均每段 {avg:.1f} 項），總成本 ${total_cost:.4f}"
               + (f"，{unparsed} 段無法解析" if unparsed else ""))
    lines.insert(4, f"> **結論**：{summary}\n")
    logger.info("完成。%s", summary)

    out = storage.root / "eval" / f"{stock}_{year}Q{quarter}_fidelity.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    logger.info("報告已寫入 %s", out)
    return 0
