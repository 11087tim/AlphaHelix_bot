"""熱門外資研報 pipeline 主程式。

流程（每晚 23:00 排程）：
  valuelist 熱榜 → 清洗英文標題 → nash-ai 搜尋匹配 → 下載新 PDF →
  pypdf 抽文字 → LLM 單篇摘要 + 跨篇彙整 → email

狀態存 hot_reports_data/state.json（以 valuelist url 為 key），
已下載/已摘要的不重做；餘額不足的下次自動重試。

CLI:
  python -m hot_reports.main run             # 完整每日流程
  python -m hot_reports.main run --no-email  # 測試：不寄信
  python -m hot_reports.main run --no-llm    # 測試：只抓+下載，不做 LLM
  python -m hot_reports.main status          # 看 state 統計 + token 狀態
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import time
from datetime import datetime

from . import clean, config, digest, match, scrape
from .nash import NashClient, QuotaExhausted, TokenExpired

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("hot_reports")


def load_state() -> dict:
    if config.STATE_PATH.exists():
        return json.loads(config.STATE_PATH.read_text(encoding="utf-8"))
    return {"reports": {}}


def save_state(state: dict) -> None:
    config.STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def safe_name(s: str, limit: int = 120) -> str:
    s = re.sub(r'[\\/:*?"<>|\n]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()[:limit]


def send_digest_email(subject: str, html_body: str, dev_only: bool) -> None:
    from src.emailer import send_html_email
    cfg = config.load_email_cfg()
    to = cfg["dev"] if dev_only else (cfg["prod"] or cfg["dev"])
    if not (cfg["gmail_address"] and to):
        logger.warning("email 設定不完整，略過寄信")
        return
    send_html_email(
        gmail_address=cfg["gmail_address"], gmail_app_password=cfg["gmail_app_password"],
        to=to, subject=subject, html_body=html_body)


def _md_to_html(md: str) -> str:
    """極簡 markdown → HTML（標題/粗體/列點/換行），寄信用。"""
    out = html.escape(md)
    out = re.sub(r'^### (.+)$', r'<h3>\1</h3>', out, flags=re.M)
    out = re.sub(r'^## (.+)$', r'<h2>\1</h2>', out, flags=re.M)
    out = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', out)
    out = re.sub(r'^- (.+)$', r'<li>\1</li>', out, flags=re.M)
    out = re.sub(r'(<li>.*?</li>\n?)+', lambda m: '<ul>' + m.group(0) + '</ul>', out, flags=re.S)
    return out.replace('\n', '<br>\n')


def run(no_email: bool, no_llm: bool, dev_email: bool) -> int:
    from dotenv import load_dotenv
    load_dotenv(config.PROJECT_ROOT / ".env")
    config.ensure_dirs()
    state = load_state()
    reports = state["reports"]
    today = datetime.now().strftime("%Y-%m-%d")
    events: list[str] = []          # 給 email 的執行摘要
    token_expired = False
    quota_exhausted = False

    # --- 1. 抓熱榜 + 清洗 ---
    rows = scrape.scrape_hot_titles()
    items = clean.clean_rows(rows)
    new_items = [it for it in items if it["url"] not in reports]
    for it in new_items:
        it.update({"first_seen": today, "status": "new"})
        reports[it["url"]] = it
    logger.info("熱榜 %d 篇（去重後），新出現 %d 篇", len(items), len(new_items))
    events.append(f"熱榜 {len(items)} 篇，新出現 {len(new_items)} 篇")

    # --- 2. token 檢查（失效時若 .env 有帳密就自動重登）---
    client = NashClient(config.read_token())
    if not client.token or not client.token_valid():
        import os
        phone, pw = os.environ.get("NASH_PHONE"), os.environ.get("NASH_PASSWORD")
        if phone and pw:
            try:
                from .nash import login_with_password
                client = NashClient(login_with_password(phone, pw))
                config.TOKEN_PATH.write_text(client.token)
                logger.info("token 已用帳密自動續期")
            except Exception as exc:
                logger.warning("帳密自動登入失敗：%s", exc)
        if not client.token or not client.token_valid():
            token_expired = True
            events.append("⚠️ nash-ai token 失效：跳過搜尋/下載（標題已入庫，token 更新後下次自動補）")
            logger.warning("token 失效，跳過 nash-ai 階段")

    # --- 3. 匹配（只處理還沒匹配過的 + 有英文標題的）---
    if not token_expired:
        pending_match = [r for r in reports.values()
                         if r.get("status") == "new" and r.get("title_en")]
        for r in pending_match:
            try:
                res = match.match_one(client, r)
            except TokenExpired:
                token_expired = True
                events.append("⚠️ token 中途失效，剩餘匹配延到下次")
                break
            except Exception as exc:
                logger.warning("匹配失敗（%s）：%s", r["title_en"][:40], exc)
                continue
            r.update(res)
            r["status"] = {"match": "matched", "weak": "no_match", "none": "no_match"}[res["status"]]
            logger.info("匹配 %s (%.2f) %s", res["status"], res["score"], r["title_en"][:50])
        # 沒英文標題的直接標記
        for r in reports.values():
            if r.get("status") == "new" and not r.get("title_en"):
                r["status"] = "no_english"
        save_state(state)

    # --- 4. 下載（matched 但還沒有 pdf 的，含之前餘額不足的；小檔優先省額度）---
    downloaded: list[dict] = []
    if not token_expired:
        pending_dl = [r for r in reports.values() if r.get("status") == "matched"]
        pending_dl.sort(key=lambda r: int(r.get("nash_pages") or 999))
        for r in pending_dl:
            fname = safe_name(f"{r['nash_date']}_{r['nash_securities'] or ''}_{r['nash_title']}") + ".pdf"
            path = config.PDF_DIR / fname
            try:
                data = client.download_pdf(r["nash_id"])
            except QuotaExhausted:
                quota_exhausted = True
                logger.warning("頁數餘額不足，停止下載（剩 %d 篇待補）",
                               sum(1 for x in pending_dl if x.get("status") == "matched"))
                break
            except TokenExpired:
                token_expired = True
                break
            except Exception as exc:
                logger.warning("下載失敗（id=%s）：%s", r["nash_id"], exc)
                continue
            path.write_bytes(data)
            r["pdf"] = fname
            r["status"] = "downloaded"
            downloaded.append(r)
            logger.info("下載完成 %dKB %s", len(data) // 1024, fname[:60])
            save_state(state)
            time.sleep(config.DOWNLOAD_DELAY_SEC)
        remaining = sum(1 for x in reports.values() if x.get("status") == "matched")
        events.append(f"下載 {len(downloaded)} 篇" +
                      (f"，餘額不足尚欠 {remaining} 篇（儲值後自動補）" if quota_exhausted else ""))

    # --- 5. LLM：抽文字 + 單篇摘要（downloaded 且未摘要的）---
    summaries_new: list[dict] = []
    if not no_llm:
        from reports.llm import get_api_key
        api_key = get_api_key()
        pending_sum = [r for r in reports.values()
                       if r.get("status") == "downloaded" and not r.get("summary")]
        for r in pending_sum:
            text = digest.extract_text(config.PDF_DIR / r["pdf"])
            if len(text) < 500:
                r["status"] = "text_failed"
                logger.warning("抽不到文字：%s", r["pdf"])
                continue
            (config.TEXT_DIR / (r["pdf"][:-4] + ".txt")).write_text(text, encoding="utf-8")
            try:
                r["summary"] = digest.summarize_report(r, text, api_key)
            except Exception as exc:
                logger.warning("摘要失敗（%s）：%s", r["pdf"][:40], exc)
                continue
            r["status"] = "summarized"
            summaries_new.append(r)
            save_state(state)
        events.append(f"LLM 摘要 {len(summaries_new)} 篇")

    # --- 6. 跨篇彙整 + email ---
    synth_md = ""
    if summaries_new and not no_llm:
        try:
            synth_md = digest.synthesize(
                [{"title": r.get("nash_title") or r.get("title_en"),
                  "securities": r.get("nash_securities") or r.get("institution"),
                  "summary": r["summary"]} for r in summaries_new],
                api_key)
        except Exception as exc:
            logger.warning("跨篇彙整失敗：%s", exc)

    if synth_md or summaries_new:
        md_parts = [f"# {today} 熱門研報彙整\n"]
        if synth_md:
            md_parts.append(synth_md)
        for r in summaries_new:
            md_parts.append(f"\n---\n## {r.get('nash_securities') or ''}｜"
                            f"{r.get('nash_title') or r.get('title_en')}\n\n{r['summary']}")
        (config.DIGEST_DIR / f"{today}.md").write_text("\n".join(md_parts), encoding="utf-8")

    if not no_email:
        parts = [f"<p>{' ｜ '.join(html.escape(e) for e in events)}</p>"]
        if token_expired:
            parts.append("<p><b>⚠️ 請更新 token</b>：瀏覽器登入 nash-ai 後，在 Mac 執行 "
                         "<code>bash hot_reports/push_token.sh</code></p>")
        if synth_md:
            parts.append("<hr><h1>今日研報彙整</h1>" + _md_to_html(synth_md))
        if summaries_new:
            parts.append("<hr><h1>單篇摘要</h1>")
            for r in summaries_new:
                parts.append(f"<h2>{html.escape(r.get('nash_securities') or '')}｜"
                             f"{html.escape((r.get('nash_title') or '')[:100])}</h2>"
                             + _md_to_html(r["summary"]))
        if summaries_new or token_expired or quota_exhausted:
            send_digest_email(f"[熱門研報] {today} 新增 {len(summaries_new)} 篇",
                              "\n".join(parts), dev_only=dev_email)
        else:
            logger.info("無新內容且無警示，不寄信")

    save_state(state)
    logger.info("完成：%s", "；".join(events))
    return 0


def status() -> int:
    state = load_state()
    from collections import Counter
    cnt = Counter(r.get("status") for r in state["reports"].values())
    print(f"共 {len(state['reports'])} 篇：", dict(cnt))
    client = NashClient(config.read_token())
    print("token:", "有效" if client.token and client.token_valid() else "失效/未設定")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--no-email", action="store_true")
    p_run.add_argument("--no-llm", action="store_true")
    p_run.add_argument("--dev-email", action="store_true", help="只寄 dev 收件人")
    sub.add_parser("status")
    args = ap.parse_args()
    if args.cmd == "run":
        return run(args.no_email, args.no_llm, args.dev_email)
    return status()


if __name__ == "__main__":
    sys.exit(main())
