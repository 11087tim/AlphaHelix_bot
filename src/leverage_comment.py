"""LLM 去槓桿壓力短評：從全市場個股數據挑壓力最大的個股（大市值優先，最多 20 檔）。

流程：程式先算壓力分數做候選預篩（~40 檔、含大市值加權）→ LLM 依數據挑選並寫短評
→ 存 data/leverage/llm_comment.json → dashboard 嵌在個股表上方。
挑選與理由都必須基於提供的數據欄位，禁止捏造（fact-based）。
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

if __package__:
    from .summarizer import _post_chat
else:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.summarizer import _post_chat

logger = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent.parent / "data" / "leverage"
OUT = DATA / "llm_comment.json"
CALL = 130.0

SYSTEM = """你是台股槓桿風險分析師。你會收到一批個股的去槓桿壓力數據（皆為實際數據，非估計），欄位：
- weight：市值佔大盤比重%（越大、對大盤影響越大）
- mratio：融資佔市值%（籌碼中融資比重，易燃物）
- chg5：近5交易日漲跌%（火苗，負=正在跌）
- dist：距追繳%（還能跌多少觸及融資追繳線130%；負值=已在追繳，引信）
- rank52：融資餘額52週百分位（100=一年最高檔，燃料水位）
- mbal：融資餘額（張）
- liq：可能斷頭日（維持率連續跌破130的起始日+3營業日；null=尚未跌破）
另會提供【市場層級的跌破130統計】（近8個交易日跌破家數/融資部位市值、目前跌破者的首次跌破日分布）。
台灣融資追繳機制：維持率跌破130% → 當日盤後發追繳通知 → T+2個營業日內須補繳 → 未補繳者於第3個營業日【開盤】處分（斷頭賣壓集中在開盤）；期間若維持率反彈回130以上則暫緩。
任務：
1. SUMMARY 必須包含對【斷頭潮】的判斷：依跌破統計與機制推論——斷頭賣壓是否已開始、主要賣壓大概率落在哪幾個日期（用具體日期）、規模多大（融資部位市值）。
2. 挑出「去槓桿壓力最大」的個股，【大市值優先】（同等壓力下 weight 大者優先入選、排前面），最多 20 檔。
只能從輸入名單挑選、理由只能引用給你的數據，禁止使用任何外部知識或編造產業/新聞背景。
理由與總評一律用繁體中文書寫，引用數據時用中文欄位名——市值比重(weight)、融資佔市值(mratio)、近5日(chg5)、距追繳(dist)、52週分位(rank52)、融資餘額(mbal)——【不要】在輸出中出現 weight/mratio/chg5/dist/rank52/mbal 這些英文代號。範例理由：「市值比重0.89%為權值股，距追繳僅0.8%、近5日跌22.3%，隨時觸及追繳線」。
輸出格式（純文字、每行一項、不要其他內容、不要 markdown）：
SUMMARY: 3-4 句整體短評（含斷頭潮判斷：是否已開始、主要賣壓日期與規模；以及壓力集中在哪、該注意什麼）
PICK: 代號|一句話理由（引用具體數據）
PICK: 代號|...（依壓力大小排序，最多 20 行）"""


def _load(name):
    return json.loads((DATA / f"{name}.json").read_text())


def _breach_stats():
    """跌破130統計：近8日家數/部位市值 + 目前跌破者首次跌破日分布 + 個股可能斷頭日。"""
    from datetime import date as _date, timedelta as _td

    def _add_bd(dstr, n):
        d = _date.fromisoformat(dstr)
        c = 0
        while c < n:
            d += _td(days=1)
            if d.weekday() < 5:
                c += 1
        return d.isoformat()

    h = json.loads((DATA / "tej_hist.json").read_text())
    dates, S = h["dates"], h["stocks"]
    mg = _load("mkt_margin")
    D = max(r["d"] for r in mg)
    mbal = {r["id"]: r["mbal"] for r in mg if r["d"] == D}
    px = {r["id"]: r["c"] for r in _load("mkt_price") if r["d"] == D and r.get("c")}

    daily = []
    for d in dates[-8:]:
        i = dates.index(d)
        c, v = 0, 0.0
        for sid, arr in S.items():
            if arr[i] is not None and arr[i] < CALL:
                c += 1
                v += mbal.get(sid, 0) * 1000 * px.get(sid, 0) / 1e8
        daily.append({"date": d, "breached": c, "value_yi": round(v)})

    li = len(dates) - 1
    cohort, liq = {}, {}
    for sid, arr in S.items():
        if arr[li] is None or arr[li] >= CALL:
            continue
        j = li
        while j > 0 and arr[j - 1] is not None and arr[j - 1] < CALL:
            j -= 1
        d0 = dates[j]
        liq[sid] = _add_bd(d0, 3)
        e = cohort.setdefault(d0, {"n": 0, "value_yi": 0.0})
        e["n"] += 1
        e["value_yi"] += mbal.get(sid, 0) * 1000 * px.get(sid, 0) / 1e8
    cohorts = [{"first_breach": d, "liq_date": _add_bd(d, 3), "n": e["n"],
                "value_yi": round(e["value_yi"])} for d, e in sorted(cohort.items())[-8:]]
    return daily, cohorts, liq


def _candidates():
    """程式側預篩：壓力分數 ×（市值加權），回傳 (table_date, top40 資料列)。"""
    mg = _load("mkt_margin")
    table_date = max(r["d"] for r in mg)
    mg_by = defaultdict(list)
    for r in mg:
        mg_by[r["id"]].append(r)
    mv_rows = _load("mkt_mktval")
    mv_date = max(r["d"] for r in mv_rows)
    mv_by = {r["id"]: r["mv"] for r in mv_rows if r["d"] == mv_date}
    mv_total = sum(mv_by.values()) or 1
    maint = json.loads((DATA / "mkt_maintenance.json").read_text()).get("ratio", {})
    names = json.loads((DATA / "names.json").read_text()) if (DATA / "names.json").exists() else {}
    ph = defaultdict(list)
    for r in _load("mkt_price"):
        if r.get("c"):
            ph[r["id"]].append((r["d"], r["c"]))

    def clip(x, lo=0.0, hi=1.0):
        return max(lo, min(hi, x))

    rows = []
    for sid, recs in mg_by.items():
        recs.sort(key=lambda r: r["d"])
        b = recs[-1]
        if b["d"] != table_date or b["mbal"] < 500:  # 太小的融資部位不看
            continue
        mbal = b["mbal"]
        weight = mv_by.get(sid, 0) / mv_total * 100
        if weight < 0.01:  # 市值太小者排除（大市值為主）
            continue
        M = maint.get(sid, 0)
        dist = (M - CALL) / M * 100 if M > 0 else None
        p = sorted(ph.get(sid, []))
        px = p[-1][1] if p else 0
        mv = mv_by.get(sid, 0)
        mratio = mbal * 1000 * px / mv * 100 if mv and px else 0
        chg5 = (p[-1][1] / p[-6][1] - 1) * 100 if len(p) >= 6 else 0
        hist = [r["mbal"] for r in recs[-252:]]
        rank52 = sum(1 for v in hist if v <= mbal) / len(hist) * 100 if len(hist) >= 30 else 50
        # 壓力分數（引信 45%＋火苗 30%＋易燃 15%＋燃料 10%），乘市值權重的對數加成
        import math
        pd_ = clip((15 - dist) / 15, 0, 1.2) if dist is not None else 0
        score = (0.45 * pd_ + 0.30 * clip(-chg5 / 15) + 0.15 * clip(mratio / 10)
                 + 0.10 * clip(rank52 / 100))
        score *= 0.6 + 0.4 * math.log10(1 + weight * 20)
        rows.append({"id": sid, "name": names.get(sid, sid), "weight": round(weight, 3),
                     "mratio": round(mratio, 2), "chg5": round(chg5, 1),
                     "dist": round(dist, 1) if dist is not None else None,
                     "rank52": round(rank52), "mbal": mbal, "_s": round(score, 4)})
    rows.sort(key=lambda r: -r["_s"])
    return table_date, rows[:40]


def _attach_liq(cands, liq):
    for r in cands:
        r["liq"] = liq.get(r["id"])


def generate(api_key: str, model: str) -> dict:
    """產生短評並寫入 llm_comment.json；回傳結果 dict。"""
    table_date, cands = _candidates()
    daily, cohorts, liq = _breach_stats()
    _attach_liq(cands, liq)
    user = ("資料日期：%s（單位如欄位說明）。\n"
            "【近8個交易日跌破130統計】%s\n"
            "【目前跌破者的首次跌破日分布（liq_date=推定處分日）】%s\n"
            "候選名單（已按程式壓力分數初排，你需自行判斷取捨與排序）：\n%s"
            % (table_date,
               json.dumps(daily, ensure_ascii=False),
               json.dumps(cohorts, ensure_ascii=False),
               json.dumps([{k: v for k, v in r.items() if k != "_s"} for r in cands],
                          ensure_ascii=False)))
    payload = {"model": model,
               "messages": [{"role": "system", "content": SYSTEM},
                            {"role": "user", "content": user}]}
    data = _post_chat(api_key, payload)
    text = data["choices"][0]["message"]["content"].strip()
    summary, byid, picks = "", {r["id"]: r for r in cands}, []
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("SUMMARY:"):
            summary = line.split(":", 1)[1].strip()
        elif line.upper().startswith("PICK:") and len(picks) < 20:
            body = line.split(":", 1)[1].strip()
            sid, _, reason = body.partition("|")
            r = byid.get(sid.strip())
            if not r:  # 只接受候選名單內的（防幻覺）
                continue
            picks.append({"id": r["id"], "name": r["name"], "weight": r["weight"],
                          "mratio": r["mratio"], "chg5": r["chg5"], "dist": r["dist"],
                          "reason": reason.strip()[:120]})
    if not picks:
        raise ValueError("LLM 輸出無有效 PICK 行：%s" % text[:200])
    out = {"date": table_date, "model": model,
           "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
           "summary": summary[:500], "picks": picks}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("LLM 短評完成：%d 檔（%s）", len(picks), model)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from src.config import load_config

    cfg = load_config()
    res = generate(cfg.openrouter_api_key, cfg.openrouter_model)
    print(res["summary"])
    for p in res["picks"]:
        print(f"  {p['id']} {p['name']}: {p['reason']}")
