from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import ReportsConfig
from . import finmind_client, llm
from .storage import ReportStorage

logger = logging.getLogger(__name__)

# 只餵附註中與「合約負債→未來營收」相關的段落，不是全文分析。
# 關鍵字命中行 ±WINDOW 行取窗、重疊合併，控制送進模型的字數。
_KEYWORDS = [
    "合約負債", "合約資產", "客戶合約", "履約義務", "剩餘履約義務",
    "收入認列", "預收", "收入之細分", "部門資訊",
    "營業收入", "每股盈餘", "本期淨利",
    # 毛利率先行訊號：存貨評價、虧損性合約、減損
    "存貨", "跌價", "虧損性合約", "減損",
]
_WINDOW = 30
_MAX_CHARS = 50000

EXTRACT_SYSTEM = (
    "你是財報分析師。以下是台股公司某一季財報中，與「合約負債／客戶合約收入」相關的節錄段落"
    "（關鍵字視窗擷取，段落間可能不連續）。請整理成一張精簡的事實卡，只列有揭露的項目，"
    "沒揭露的明確寫「未揭露」，絕不杜撰數字：\n"
    "1. 合約負債餘額：流動/非流動、本期末 vs 上期末（含比較期日期；財報用民國年，請換算西元）。\n"
    "2. 合約負債的組成/性質（例如設備銷售預收款、工程預收款；引用附註原文用詞）。\n"
    "3. 期初合約負債於本期認列為收入之金額（若有揭露）。\n"
    "4. 收入認列政策：一時點 vs 隨時間逐步認列（完工百分比）；各類收入的認列方式。\n"
    "5. 剩餘履約義務（未滿足之履約義務）金額與預期認列時程（若有揭露，年報較常見）。\n"
    "6. 本期營業收入、營業成本、毛利率、本期淨利(損)、基本每股盈餘——並標注期間（單季或累計）。\n"
    "7. 收入細分/部門別收入（若有）。\n"
    "8. 毛利率先行訊號：(a)存貨備抵跌價之本期提列或迴轉金額（計入銷貨成本者）；"
    "(b)存貨組成（原料/在製品/製成品各多少）；(c)虧損性合約準備之提列/迴轉；"
    "(d)合約資產減損（備抵損失）之提列/迴轉。以上各項若無揭露寫「未揭露」。\n"
    "繁體中文、條列、保留所有具體數字與單位（新台幣仟元）。"
)

SYNTH_SYSTEM = (
    "你是資深投資分析師。以下是同一家台股公司連續數季的「合約負債事實卡」（依時間排序，"
    "每季一段，內容皆出自財報附註節錄）。這家公司是因『最新一季合約負債跳升』被篩選出來的，"
    "請寫一份「合約負債深讀報告」回答三個核心問題：\n"
    "1. 【合約性質】這些合約負債是什麼性質的合約（產品/工程/服務？預收款慣例？），"
    "跳升是單一大單、季節性、還是結構性成長？\n"
    "2. 【認列時點】預計何時認列為營收？優先用「期初合約負債本期認列收入」與剩餘履約義務揭露；"
    "若無，就用跨季『合約負債餘額 → 次期營收』的歷史關係推估轉換速度（幾季轉完、轉換率）。\n"
    "3. 【營收/EPS 推估】以最新合約負債餘額 × 歷史轉換節奏，推估未來 1～2 季營收增量區間；"
    "再套歷史毛利率與費用結構，推估對 EPS 的影響區間。所有假設明列，給區間不給單點。\n"
    "另外：\n"
    "4. 【毛利率訊號】用四個先行指標判讀毛利率方向：(a)單季毛利率逐季序列與轉折"
    "（Q4 單季＝全年減前三季累計，自行換算）；(b)存貨備抵跌價提列/迴轉趨勢與存貨組成變化"
    "（製成品堆高=滯銷、原料/在製品增加且合約負債同升=健康趕單）；(c)虧損性合約準備的提列/迴轉；"
    "(d)合約資產減損變動（注意：減損迴轉墊高的毛利是品質較差的改善，要標明）。"
    "綜合給出方向判斷：改善/惡化/不明，並說明依據季度。\n"
    "5. 紅旗與不確定性（例如合約負債同時伴隨合約資產減損、退款條款、客戶集中）。\n"
    "6. 最後一行用「一句話：」總結投資意涵。\n"
    "鐵律：只根據提供內容，數字要標注出處季度（如「2026Q1 附註」）；推估值要明確標示為推估。"
    "若輸入含「程式計算數據」段（以 FinMind 股本/月營收＋事實卡數字由程式算出），"
    "營收/EPS 推估【必須直接採用該段的數值與區間，不得自行心算或另立數字】；"
    "你的工作是解讀與挑戰這些數字（合理性、與附註是否衝突），不是重算。"
    "程式數據與附註衝突時，明確指出衝突並說明採信哪邊。"
    "繁體中文，小標＋條列。"
)

TO_JSON_SYSTEM = (
    "你是資料轉換器。以下是一張財報事實卡（markdown）。請輸出「單一 JSON 物件」，不要任何其他文字："
    '{"contract_liability": 期末流動合約負債仟元或null, '
    '"opening_cl_recognized": 期初合約負債本期認列收入仟元或null, '
    '"opening_cl_recognized_period": "single"或"cumulative"或null, '
    '"gross_margin_pct": 毛利率百分比數值或null, '
    '"gross_margin_period": "single"或"cumulative"或null, '
    '"net_income": 本期淨利仟元或null(淨損為負), '
    '"eps": 基本每股盈餘元或null}。'
    "規則：卡片寫「未揭露」或沒提到就填 null；金額一律仟元；不要編造。"
)


def _excerpt(text: str) -> str:
    """關鍵字視窗擷取：命中行 ±WINDOW 行，重疊區間合併。"""
    lines = text.splitlines()
    hits = [i for i, ln in enumerate(lines) if any(k in ln.replace(" ", "") for k in _KEYWORDS)]
    if not hits:
        return ""
    ranges: list[list[int]] = []
    for i in hits:
        lo, hi = max(0, i - _WINDOW), min(len(lines), i + _WINDOW + 1)
        if ranges and lo <= ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], hi)
        else:
            ranges.append([lo, hi])
    parts = ["\n".join(lines[lo:hi]) for lo, hi in ranges]
    out = "\n\n…（跳過無關段落）…\n\n".join(parts)
    return out[:_MAX_CHARS]


def _done_quarters(storage: ReportStorage, stock: str, language: str) -> list[tuple[int, int]]:
    qs = []
    for e in storage.manifest.values():
        if e.get("status") == "done" and e.get("co_id") == stock and e.get("language") == language:
            qs.append((e["year"], e["quarter"]))
    return sorted(set(qs))


def _extract_quarter(cfg: ReportsConfig, storage: ReportStorage, stock: str,
                     year: int, q: int, api_key: str):
    """單季：文字檔 → 關鍵字節錄 → 便宜模型事實卡。已存在就直接讀。"""
    card_path = storage.root / "analysis" / f"{stock}_clq_{year}Q{q}_{cfg.language}.md"
    if card_path.exists():
        return card_path.read_text(encoding="utf-8")
    entry = storage.manifest.get(storage.key(stock, year, q, cfg.language)) or {}
    rt = entry.get("report_type", "consolidated")
    txt_path = storage.text_dir / stock / f"{year}Q{q}_{rt}_{cfg.language}.txt"
    if not txt_path.exists():
        raise FileNotFoundError(f"缺文字檔：{txt_path}（請先 extract）")
    excerpt = _excerpt(txt_path.read_text(encoding="utf-8"))
    if not excerpt:
        raise RuntimeError(f"{stock} {year}Q{q} 找不到合約負債相關段落")
    res = llm.chat(cfg.cheap_model, EXTRACT_SYSTEM,
                   f"公司：{stock}，季度：{year}Q{q}（民國 {year - 1911} 年）\n\n{excerpt}", api_key)
    logger.info("  %dQ%d 事實卡（%s）✓ $%.4f", year, q, cfg.cheap_model, res.get("cost") or 0)
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text(res["text"], encoding="utf-8")
    return res["text"]


def _card_to_json(cfg: ReportsConfig, storage: ReportStorage, stock: str,
                  year: int, q: int, card: str, api_key: str) -> dict:
    """事實卡 → 結構化數字（便宜模型），檔案快取。解析失敗回空 dict。"""
    jpath = storage.root / "analysis" / f"{stock}_clj_{year}Q{q}.json"
    if jpath.exists():
        return json.loads(jpath.read_text(encoding="utf-8"))
    res = llm.chat(cfg.cheap_model, TO_JSON_SYSTEM, card, api_key)
    m = re.search(r"\{.*\}", res["text"], re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        logger.warning("  %dQ%d 事實卡 JSON 解析失敗", year, q)
        data = {}
    jpath.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


_Q_MONTHS = {1: (1, 2, 3), 2: (4, 5, 6), 3: (7, 8, 9), 4: (10, 11, 12)}


def _quarter_rev(months: list[dict], year: int, q: int) -> int | None:
    """由 FinMind 月營收（元）加總單季營收，回傳仟元；缺月回 None。"""
    vals = [r["revenue"] for r in months if r["year"] == year and r["month"] in _Q_MONTHS[q]]
    return round(sum(vals) / 1000) if len(vals) == 3 else None


def _compute_block(stock: str, ordered: list[tuple[int, int]], jcards: dict) -> str:
    """程式計算層：FinMind 股本/月營收 ＋ 事實卡數字 → 轉換率、營收/EPS 情境（可回溯公式）。
    回傳 markdown 區塊；FinMind 取用失敗時回退為僅事實卡可算的部分。"""
    lines = ["### 程式計算數據（推估時必須採用，勿自行心算）", ""]
    shares, months = None, []
    try:
        token = finmind_client.get_token()
        shares = finmind_client.latest_shares(stock, token)
        months = finmind_client.month_revenue(stock, token)
    except Exception as exc:  # noqa: BLE001
        logger.warning("  FinMind 取用失敗（退回純事實卡計算）：%s", exc)

    # 逐季表：合約負債 / 單季營收(FinMind月營收加總) / 毛利率 / EPS
    lines.append("| 季度 | 期末合約負債(仟元) | 單季營收(仟元,FinMind月營收加總) | 毛利率(事實卡) | EPS(事實卡) |")
    lines.append("|---|---|---|---|---|")
    rev_by_q, cl_by_q = {}, {}
    for y, q in ordered:
        j = jcards.get((y, q)) or {}
        cl = j.get("contract_liability")
        rev = _quarter_rev(months, y, q) if months else None
        cl_by_q[(y, q)], rev_by_q[(y, q)] = cl, rev
        gm = j.get("gross_margin_pct")
        gm_s = f"{gm}%({j.get('gross_margin_period') or '?'})" if gm is not None else "—"
        lines.append(f"| {y}Q{q} | {cl if cl is not None else '—'} | {rev if rev is not None else '—'} "
                     f"| {gm_s} | {j.get('eps') if j.get('eps') is not None else '—'} |")

    # 轉換率：優先揭露值（期初合約負債認列/期初餘額），並列 proxy（次季營收/當季期末合約負債）
    lines.append("")
    disclosed = []
    for i, (y, q) in enumerate(ordered[1:], 1):
        j = jcards.get((y, q)) or {}
        rec, prev_cl = j.get("opening_cl_recognized"), cl_by_q.get(ordered[i - 1])
        # 只採單季口徑：累計值的分母是年初餘額而非上季期末，混用會嚴重失真
        if rec and prev_cl and j.get("opening_cl_recognized_period") == "single":
            disclosed.append((f"{y}Q{q}", rec / prev_cl, f"{rec}/{prev_cl}"))
    if disclosed:
        lines.append("**揭露轉換率**（單季口徑：期初合約負債本季認列 ÷ 上季期末餘額）：")
        lines += [f"- {qs}: {r:.1%} ＝ {f}" for qs, r, f in disclosed]
    proxies = []
    for i in range(len(ordered) - 1):
        cl, nrev = cl_by_q.get(ordered[i]), rev_by_q.get(ordered[i + 1])
        if cl and nrev:
            proxies.append((f"{ordered[i][0]}Q{ordered[i][1]}→次季", nrev / cl))
    if proxies:
        lines.append("**Proxy 比值**（次季營收 ÷ 當季期末合約負債；僅供轉換節奏參考，>1 代表營收多數來自非預收）：")
        lines += [f"- {qs}: {r:.2f}" for qs, r in proxies]

    # 情境計算：最新合約負債 × 轉換率區間 × 毛利率區間 → EPS 貢獻
    latest = ordered[-1]
    cl_latest = cl_by_q.get(latest)
    rates = [r for _, r, _ in disclosed[-2:]] or [min(1.0, max(0.1, r)) for _, r in proxies[-3:]]
    # 毛利率限近四季正值：反映當前結構，避免被久遠高毛利期撐大區間
    gms = [jcards[k].get("gross_margin_pct") for k in ordered[-4:] if (jcards.get(k) or {}).get("gross_margin_pct")]
    gms = [g / 100 for g in gms if g and g > 0]
    if cl_latest and rates and gms and shares:
        r_lo, r_hi = min(rates), max(rates)
        g_lo, g_hi = min(gms), max(gms)
        inc_lo, inc_hi = cl_latest * r_lo, cl_latest * r_hi
        eps_lo = inc_lo * g_lo * 0.8 * 1000 / shares["shares"]
        eps_hi = inc_hi * g_hi * 0.8 * 1000 / shares["shares"]
        lines += ["", "**情境計算**（下一季，由存量合約負債轉出）：",
                  f"- 股數：{shares['shares']:,} 股（FinMind 股本 {shares['capital_ntd']:,.0f} 元 ÷ 面額10，{shares['date']}期末，非加權平均）",
                  f"- 營收增量 ＝ {cl_latest:,} × 轉換率 [{r_lo:.0%}, {r_hi:.0%}] ＝ [{inc_lo:,.0f}, {inc_hi:,.0f}] 仟元",
                  f"- EPS 貢獻 ＝ 增量 × 毛利率 [{g_lo:.1%}, {g_hi:.1%}] × 0.8(稅) ÷ 股數 ＝ **[{eps_lo:.2f}, {eps_hi:.2f}] 元/股**",
                  "- 注意：EPS 貢獻未扣「隨營收增加的營業費用」，屬上緣估計；毛利率區間取自事實卡正值毛利率的極值。"]

    # 財報後最新月營收：即時驗證轉換是否已發生
    if months:
        last_y, last_q = latest
        after = [r for r in months if (r["year"], r["month"]) > (last_y, _Q_MONTHS[last_q][-1])][-3:]
        if after:
            lines.append("")
            lines.append("**財報季後最新月營收（FinMind，仟元）— 用來即時驗證合約負債是否已在轉營收：**")
            for r in after:
                yoy = [x["revenue"] for x in months if x["year"] == r["year"] - 1 and x["month"] == r["month"]]
                yoy_s = f"，YoY {r['revenue'] / yoy[0] - 1:+.0%}" if yoy and yoy[0] else ""
                lines.append(f"- {r['year']}/{r['month']:02d}: {r['revenue'] / 1000:,.0f}{yoy_s}")
    return "\n".join(lines)


def run_contract_liability(cfg: ReportsConfig, stock: str, n_quarters: int = 8) -> int:
    storage = ReportStorage(cfg.data_dir)
    quarters = _done_quarters(storage, stock, cfg.language)[-n_quarters:]
    if not quarters:
        logger.error("找不到 %s 已下載的財報，請先 fetch。", stock)
        return 1
    api_key = llm.get_api_key()
    logger.info("合約負債深讀 %s 近 %d 季：%s", stock, len(quarters),
                "、".join(f"{y}Q{q}" for y, q in quarters))

    cards: dict[tuple[int, int], str] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(quarters))) as ex:
        futs = {ex.submit(_extract_quarter, cfg, storage, stock, y, q, api_key): (y, q)
                for y, q in quarters}
        for fut in as_completed(futs):
            y, q = futs[fut]
            try:
                cards[(y, q)] = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("  %dQ%d 事實卡失敗：%s", y, q, exc)

    if not cards:
        logger.error("沒有可用的事實卡。")
        return 1

    ordered = sorted(cards)

    # 結構化數字（便宜模型、有快取）→ 程式計算層
    jcards: dict[tuple[int, int], dict] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_card_to_json, cfg, storage, stock, y, q, cards[(y, q)], api_key): (y, q)
                for y, q in ordered}
        for fut in as_completed(futs):
            jcards[futs[fut]] = fut.result()
    calc_block = _compute_block(stock, ordered, jcards)
    calc_path = storage.root / "analysis" / f"{stock}_cl_calc.md"
    calc_path.write_text(calc_block, encoding="utf-8")
    logger.info("  程式計算層 ✓ %s", calc_path.name)

    body = "\n\n".join(f"===== {y}Q{q} =====\n{cards[(y, q)]}" for y, q in ordered)
    res = llm.chat(cfg.strong_model, SYNTH_SYSTEM,
                   f"公司：{stock}\n涵蓋季度：{'、'.join(f'{y}Q{q}' for y, q in ordered)}"
                   f"（最新一季 = 篩選訊號季）\n\n{calc_block}\n\n{body}", api_key)
    logger.info("  跨季深讀（%s）✓ $%.4f", cfg.strong_model, res.get("cost") or 0)

    span = f"{ordered[0][0]}Q{ordered[0][1]}_to_{ordered[-1][0]}Q{ordered[-1][1]}"
    out = storage.root / "analysis" / f"{stock}_contract_liability_{span}_{cfg.language}.md"
    lines = [f"# {stock} 合約負債深讀（{ordered[0][0]}Q{ordered[0][1]}–{ordered[-1][0]}Q{ordered[-1][1]}）",
             f"<sub>{cfg.cheap_model} 逐季萃取 · {cfg.strong_model} 跨季判讀 · 來源：MOPS 財報附註節錄</sub>",
             "", res["text"]]
    out.write_text("\n".join(lines), encoding="utf-8")
    logger.info("完成。深讀報告已寫入 %s", out)
    return 0
