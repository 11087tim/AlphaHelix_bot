"""月營收 nowcast：用已公布月份＋歷史季節形狀，估當季/次季營收與 EPS 基線。

    python -m reports.main nowcast [股號 ...]   # 未給股號則跑 config 全清單

方法（純程式、零 LLM）：
1. 當季：已公布月份合計 ÷ 歷史同季「已公布月份佔比」（近 3 年 min/max 佔比 → 區間）。
2. 次季：歷史 QoQ 季節因子（近 3 年 min/max）套在當季估計上。
3. EPS 基線：營收估計 × 毛利率區間（cl 事實卡 JSON 近 4 季正值）× 0.8(稅) ÷ FinMind 股本。
   未跑過 cl 的股票只出營收估計。
輸出：reports_data/analysis/<股號>_nowcast.md ＋ 終端摘要表。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from .config import ReportsConfig
from . import finmind_client

logger = logging.getLogger(__name__)

_Q_MONTHS = {1: (1, 2, 3), 2: (4, 5, 6), 3: (7, 8, 9), 4: (10, 11, 12)}


def _rev_map(months: list[dict]) -> dict[tuple[int, int], float]:
    return {(r["year"], r["month"]): r["revenue"] / 1000 for r in months}  # 仟元


def _quarter_of(y: int, m: int) -> tuple[int, int]:
    return y, (m - 1) // 3 + 1


def _next_q(y: int, q: int) -> tuple[int, int]:
    return (y + 1, 1) if q == 4 else (y, q + 1)


def _q_sum(rev: dict, y: int, q: int) -> float | None:
    vals = [rev.get((y, m)) for m in _Q_MONTHS[q]]
    return sum(vals) if all(v is not None for v in vals) else None


def nowcast_stock(stock: str, token: str, years_back: int = 3) -> dict | None:
    months = finmind_client.month_revenue(stock, token, start_date=f"{date.today().year - years_back - 1}-01-01")
    if not months:
        return None
    rev = _rev_map(months)
    last_y, last_m = max(rev)
    cy, cq = _quarter_of(last_y, last_m)
    known_months = [m for m in _Q_MONTHS[cq] if (cy, m) in rev]
    known_sum = sum(rev[(cy, m)] for m in known_months)

    # 當季估計：歷史同季「已公布月份佔比」區間
    shares = []
    for y in range(cy - years_back, cy):
        qs = _q_sum(rev, y, cq)
        if qs:
            shares.append(sum(rev.get((y, m), 0) for m in known_months) / qs)
    if len(known_months) == 3:
        cur_lo = cur_hi = known_sum
    elif shares:
        cur_lo, cur_hi = known_sum / max(shares), known_sum / min(shares)
    else:
        return None

    def _qoq_factors(q: int) -> list[float]:
        """歷史「第 q 季 → 次季」營收比值（近 years_back 年）。
        截幅 [0.3, 3.0]：微量營收/結構劇變公司的極端比值會把外推區間炸開，截掉。"""
        fs = []
        for y in range(cy - years_back, cy + 1):
            a, b = _q_sum(rev, y, q), _q_sum(rev, *_next_q(y, q))
            if a and b:
                fs.append(min(3.0, max(0.3, b / a)))
        return fs

    # T+1：歷史 QoQ 季節因子；T+2/T+3：因子鏈（每多一段區間更寬 → 信心遞減）
    ny, nq = _next_q(cy, cq)
    f1 = _qoq_factors(cq)
    nxt = (cur_lo * min(f1), cur_hi * max(f1)) if f1 else None
    n2y, n2q = _next_q(ny, nq)
    f2 = _qoq_factors(nq)
    nxt2 = (nxt[0] * min(f2), nxt[1] * max(f2)) if nxt and f2 else None
    n3y, n3q = _next_q(n2y, n2q)
    f3 = _qoq_factors(n2q)
    nxt3 = (nxt2[0] * min(f3), nxt2[1] * max(f3)) if nxt2 and f3 else None

    # 同期比較基準
    yoy_base = _q_sum(rev, cy - 1, cq)
    return {"stock": stock, "quarter": f"{cy}Q{cq}", "known_months": known_months,
            "known_sum": known_sum, "cur": (cur_lo, cur_hi), "yoy_base": yoy_base,
            "next_quarter": f"{ny}Q{nq}", "next": nxt,
            "t2_quarter": f"{n2y}Q{n2q}", "t2": nxt2,
            "t3_quarter": f"{n3y}Q{n3q}", "t3": nxt3,
            "latest_month": f"{last_y}/{last_m:02d}"}


def _cl_signal(data_dir: Path, stock: str) -> dict | None:
    """從 cl 事實卡 JSON 取：最新合約負債餘額＋單季口徑揭露轉換率（近 2 筆）區間。
    供 T+1 交叉檢核：存量轉出隱含的營收量。無資料回 None。"""
    files = sorted((data_dir / "analysis").glob(f"{stock}_clj_*.json"))
    if not files:
        return None
    seq = []
    for p in files:
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        seq.append((p.stem.split("_clj_")[1], j))
    cls = [(q, j["contract_liability"]) for q, j in seq if j.get("contract_liability")]
    if not cls:
        return None
    rates = []
    for i in range(1, len(seq)):
        j = seq[i][1]
        prev_cl = seq[i - 1][1].get("contract_liability")
        if (j.get("opening_cl_recognized") and prev_cl
                and j.get("opening_cl_recognized_period") == "single"):
            rates.append(j["opening_cl_recognized"] / prev_cl)
    latest_q, latest_cl = cls[-1]
    return {"quarter": latest_q, "cl": latest_cl,
            "rates": (min(rates[-2:]), max(rates[-2:])) if rates else None}


def _structural_signals(data_dir: Path, stock: str) -> dict | None:
    """財報結構訊號（合約負債以外的營收領先指標）：最新兩季 _clj JSON 的 QoQ 變化。
    規則票決（可回溯）：合約負債↑>20%、合約資產↑>20%、原料+在製品↑>15% 各投「上緣」一票；
    合約負債↓>20%、製成品↑>30% 各投「下緣」一票 → 綜合傾向。"""
    files = sorted((data_dir / "analysis").glob(f"{stock}_clj_*.json"))[-2:]
    if len(files) < 2:
        return None
    try:
        prev, cur = (json.loads(p.read_text(encoding="utf-8")) for p in files)
    except (json.JSONDecodeError, OSError):
        return None
    q_prev, q_cur = (p.stem.split("_clj_")[1] for p in files)

    def _pct(a, b):
        return (b - a) / a if a and b and a > 0 else None

    sig, votes = [], 0
    cl = _pct(prev.get("contract_liability"), cur.get("contract_liability"))
    if cl is not None:
        if cl > 0.2:
            votes += 1
            sig.append(("合約負債", cl, "↑ 預收堆積（上緣票）"))
        elif cl < -0.2:
            votes -= 1
            sig.append(("合約負債", cl, "↓ 預收消退（下緣票）"))
        else:
            sig.append(("合約負債", cl, "→ 平穩"))
    ca = _pct(prev.get("contract_assets"), cur.get("contract_assets"))
    if ca is not None:
        if ca > 0.2:
            votes += 1
            sig.append(("合約資產", ca, "↑ 已完工待請款，近端營收（上緣票）"))
        else:
            sig.append(("合約資產", ca, "→"))
    wip_prev = (prev.get("inventory_raw") or 0) + (prev.get("inventory_wip") or 0)
    wip_cur = (cur.get("inventory_raw") or 0) + (cur.get("inventory_wip") or 0)
    rw = _pct(wip_prev, wip_cur)
    if rw is not None:
        if rw > 0.15:
            votes += 1
            sig.append(("原料+在製品", rw, "↑ 備貨趕單（上緣票）"))
        else:
            sig.append(("原料+在製品", rw, "→"))
    fg = _pct(prev.get("inventory_fg"), cur.get("inventory_fg"))
    if fg is not None:
        if fg > 0.3:
            votes -= 1
            sig.append(("製成品", fg, "↑ 堆高，留意滯銷（下緣票）"))
        else:
            sig.append(("製成品", fg, "→"))
    if not sig:
        return None
    lean = "偏上緣" if votes > 0 else "偏下緣" if votes < 0 else "中性"
    return {"from": q_prev, "to": q_cur, "signals": sig, "votes": votes, "lean": lean}


def _pl_model(stock: str, token: str) -> dict | None:
    """完整損益結構模型（FinMind 季損益表，單季值，全程式無 LLM）：
    毛利率＝近 4 季區間；營業費用＝對營收線性擬合(斜率截距, n≥6)否則中位費用率；
    業外＝近 8 季中位數；稅率＝正稅前季的有效稅率中位(夾 10~30%)；
    母公司比率＝EPS×股數÷稅後淨利 的中位(夾 0.5~1)。
    含樣本內回測：以實際營收代入模型 vs 實際 EPS 的平均絕對誤差。"""
    q = finmind_client.quarterly_income(stock, token)
    q = [r for r in q if r.get("Revenue")]
    if len(q) < 4:
        return None
    shares_info = finmind_client.latest_shares(stock, token)
    if not shares_info:
        return None
    shares = shares_info["shares"]
    # 單位統一為仟元
    for r in q:
        for k in ("Revenue", "GrossProfit", "OperatingExpenses",
                  "TotalNonoperatingIncomeAndExpense", "PreTaxIncome", "IncomeAfterTaxes"):
            if r.get(k) is not None:
                r[k] = r[k] / 1000

    # 毛利率穩健估計：近 8 季、截尾 [-60%, 85%]、取 P25–P75 四分位距——
    # 單季減損造成的極端負毛利不再污染區間，但持續虧損的公司會誠實得到負值
    gms_raw = [r["GrossProfit"] / r["Revenue"] for r in q[-8:] if r.get("GrossProfit") and r.get("Revenue")]
    if not gms_raw:
        return None
    gs = sorted(max(-0.6, min(0.85, g)) for g in gms_raw)
    n_g = len(gs)
    gm = (gs[n_g // 4], gs[(3 * n_g) // 4]) if n_g >= 4 else (gs[0], gs[-1])
    gm_mid = gs[n_g // 2]
    volatile = min(gms_raw) < -0.1  # 曾有深度負毛利季 → 低可信標記（仍出數）

    pairs = [(r["Revenue"], r["OperatingExpenses"]) for r in q if r.get("OperatingExpenses")]
    if len(pairs) >= 6:
        n = len(pairs)
        mx = sum(p[0] for p in pairs) / n
        my = sum(p[1] for p in pairs) / n
        var = sum((p[0] - mx) ** 2 for p in pairs)
        b = sum((p[0] - mx) * (p[1] - my) for p in pairs) / var if var else 0
        b = min(max(b, 0.0), 0.6)
        a = my - b * mx
        resid = [abs(p[1] - (a + b * p[0])) for p in pairs]
        opex = {"a": a, "b": b, "err": sum(resid) / n, "mode": f"線性擬合 opex={a:,.0f}+{b:.2f}×rev"}
    elif pairs:
        ratio = sorted(p[1] / p[0] for p in pairs)[len(pairs) // 2]
        opex = {"a": 0.0, "b": ratio, "err": 0.0, "mode": f"中位費用率 {ratio:.1%}"}
    else:
        return None

    nonops = sorted(r["TotalNonoperatingIncomeAndExpense"] for r in q[-8:]
                    if r.get("TotalNonoperatingIncomeAndExpense") is not None)
    nonop = nonops[len(nonops) // 2] if nonops else 0.0
    taxes = sorted(1 - r["IncomeAfterTaxes"] / r["PreTaxIncome"] for r in q
                   if r.get("PreTaxIncome") and r["PreTaxIncome"] > 0 and r.get("IncomeAfterTaxes") is not None)
    tax = min(max(taxes[len(taxes) // 2], 0.10), 0.30) if taxes else 0.20
    ratios = sorted(r["EPS"] * shares / (r["IncomeAfterTaxes"] * 1000) for r in q
                    if r.get("EPS") and r.get("IncomeAfterTaxes") and abs(r["IncomeAfterTaxes"]) > 1)
    parent = min(max(ratios[len(ratios) // 2], 0.5), 1.0) if ratios else 1.0

    def eps_range(rev_rng):
        """掃營收×毛利率×費用殘差的極值組合，回傳排序後 (min, max)——
        負毛利率時高營收端更差，不能假設營收高=EPS 高。"""
        if not rev_rng:
            return None
        out = []
        for rev in rev_rng:
            for g in gm:
                for adj in (opex["err"], -opex["err"]):
                    op = rev * g - (opex["a"] + opex["b"] * rev + adj)
                    out.append((op + nonop) * (1 - tax) * parent * 1000 / shares)
        return (min(out), max(out))

    # 樣本內回測：近 4 季用實際營收＋模型參數 vs 實際 EPS
    errs = []
    for r in q[-4:]:
        if not (r.get("EPS") is not None and r.get("Revenue")):
            continue
        op = r["Revenue"] * gm_mid - (opex["a"] + opex["b"] * r["Revenue"])
        pred = (op + nonop) * (1 - tax) * parent * 1000 / shares
        errs.append(abs(pred - r["EPS"]))
    mae = sum(errs) / len(errs) if errs else None

    return {"gm": gm, "gm_mid": gm_mid, "opex": opex, "nonop": nonop, "tax": tax, "parent": parent,
            "shares": shares, "shares_date": shares_info["date"],
            "eps_range": eps_range, "mae": mae, "n_quarters": len(q),
            "volatile": volatile}


def run_nowcast(cfg: ReportsConfig, stocks: list[str] | None = None) -> int:
    token = finmind_client.get_token()
    stocks = stocks or cfg.stocks
    rows = []
    for s in stocks:
        try:
            nc = nowcast_stock(s, token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s nowcast 失敗：%s", s, exc)
            continue
        if not nc:
            logger.warning("%s 無足夠月營收資料", s)
            continue
        cl_sig = _cl_signal(cfg.data_dir, s)
        try:
            pl = _pl_model(s, token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 損益模型建立失敗：%s", s, exc)
            pl = None

        def _eps(rng):
            return pl["eps_range"](rng) if pl and rng else None

        eps_cur, eps_n1, eps_n2, eps_n3 = (_eps(nc["cur"]), _eps(nc["next"]),
                                           _eps(nc["t2"]), _eps(nc["t3"]))
        nc.update({"eps": eps_cur, "eps_n1": eps_n1})
        rows.append(nc)

        yoy = f"{(nc['cur'][0] + nc['cur'][1]) / 2 / nc['yoy_base'] - 1:+.0%}" if nc["yoy_base"] else "—"

        def _row(label, rng, eps_rng, conf, basis):
            rev = f"[{rng[0]:,.0f}, {rng[1]:,.0f}]" if rng else "—"
            eps_s = f"[{eps_rng[0]:.2f}, {eps_rng[1]:.2f}]" if eps_rng else "—"
            return f"| {label} | {rev} | {eps_s} | {conf} | {basis} |"

        lines = [f"# {s} 四季展望（Nowcast，至 {nc['latest_month']}）", "",
                 "| 季度 | 營收區間(仟元) | EPS 區間(元) | 信心 | 主要依據 |", "|---|---|---|---|---|",
                 _row(f"{nc['quarter']} (T)", nc["cur"], eps_cur, "高",
                      f"已公布 {len(nc['known_months'])}/3 月（合計 {nc['known_sum']:,.0f}，YoY {yoy}）"),
                 _row(f"{nc['next_quarter']} (T+1)", nc["next"], eps_n1, "中",
                      "近 3 年 QoQ 季節因子外推，未含事件調整"),
                 _row(f"{nc['t2_quarter']} (T+2)", nc["t2"], eps_n2, "低（方向參考）",
                      "季節因子鏈外推，僅供方向"),
                 _row(f"{nc['t3_quarter']} (T+3)", nc["t3"], eps_n3, "極低（方向參考）",
                      "因子鏈三段外推，區間最寬、僅供方向"), ""]
        if cl_sig:
            # 合約負債是「該季期末」餘額 → 轉換隱含的是「其次一季」的營收（cl 落後財報一季，
            # 通常對應本表的 T 而非 T+1）；若該季已有實績，直接對照驗證轉換有無兌現。
            cq_y, cq_q = int(cl_sig["quarter"][:4]), int(cl_sig["quarter"][-1])
            tgt_y, tgt_q = _next_q(cq_y, cq_q)
            tgt = f"{tgt_y}Q{tgt_q}"
            if cl_sig["rates"]:
                r_lo, r_hi = cl_sig["rates"]
                imp = (cl_sig["cl"] * r_lo, cl_sig["cl"] * r_hi)
                msg = (f"**合約負債交叉檢核**：{cl_sig['quarter']} 期末餘額 {cl_sig['cl']:,} 仟元 × "
                       f"揭露轉換率 [{r_lo:.0%}, {r_hi:.0%}] → **{tgt}** 存量轉出隱含 "
                       f"[{imp[0]:,.0f}, {imp[1]:,.0f}] 仟元。")
                if tgt == nc["quarter"] and len(nc["known_months"]) == 3:
                    ratio = nc["known_sum"] / ((imp[0] + imp[1]) / 2)
                    verdict = ("轉換大致兌現" if 0.85 <= ratio <= 1.15 else
                               "實績高於隱含（有預收以外的營收動能）" if ratio > 1.15 else
                               "實績低於隱含（轉換放慢或訂單遞延，留意）")
                    msg += f"該季實績 {nc['known_sum']:,.0f} 仟元（實績/隱含中值 ≈ {ratio:.0%}）→ **{verdict}**。"
                lines.append(msg)
            else:
                lines.append(f"**合約負債交叉檢核**：{cl_sig['quarter']} 期末餘額 {cl_sig['cl']:,} 仟元"
                             f"（無單季口徑揭露轉換率，僅供規模參考）。")
        struct = _structural_signals(cfg.data_dir, s)
        if struct:
            lines.append(f"\n**結構訊號**（{struct['from']}→{struct['to']}，合約負債以外的營收領先指標）：")
            for name, pct, note in struct["signals"]:
                lines.append(f"- {name} QoQ {pct:+.0%}：{note}")
            lines.append(f"- **綜合傾向：T+1 落點{struct['lean']}**（票決 {struct['votes']:+d}；"
                         "規則：預收/合約資產/備貨↑=上緣票，預收↓/製成品堆高=下緣票）")
        if pl:
            vol_s = ("；⚠ 近 8 季含深度負毛利季（減損/結構變動），毛利率已改用截尾四分位穩健區間，"
                     "EPS 屬**低可信**，個案判讀見 cl 深讀") if pl["volatile"] else ""
            mae_s = f"，近 4 季樣本內回測 MAE {pl['mae']:.2f} 元" if pl["mae"] is not None else ""
            lines.append(f"\n**損益模型**（FinMind 季損益表 {pl['n_quarters']} 季，全程式）：\n"
                         f"- EPS ＝ (營收 × 毛利率 [{pl['gm'][0]:.1%}, {pl['gm'][1]:.1%}]（近8季P25–P75截尾）"
                         f"− 營業費用（{pl['opex']['mode']}，殘差 ±{pl['opex']['err']:,.0f}）"
                         f"＋ 業外中位 {pl['nonop']:,.0f}）× (1−稅率 {pl['tax']:.0%}) "
                         f"× 母公司比率 {pl['parent']:.2f} ÷ {pl['shares']:,} 股"
                         f"（{pl['shares_date']} 股本）{mae_s}{vol_s}。")
        else:
            lines.append("\nEPS：FinMind 季損益資料不足（<4 季），僅出營收。")
        lines.append("\n方法：純程式計算（無 LLM）。T 用已公布月份÷近 3 年同季佔比；T+1/T+2 用季節因子；"
                     "事件層（重訊/新聞）調整待接入。")
        out = cfg.data_dir / "analysis" / f"{s}_nowcast.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines), encoding="utf-8")

    if not rows:
        logger.error("沒有任何股票產出 nowcast。")
        return 1
    print(f"{'股號':<6}{'T季度':<8}{'T營收(百萬)':<16}{'YoY':<8}{'T EPS':<14}{'T+1 EPS':<14}")
    for nc in rows:
        lo, hi = nc["cur"]
        yoy = f"{(lo + hi) / 2 / nc['yoy_base'] - 1:+.0%}" if nc["yoy_base"] else "—"
        e = f"[{nc['eps'][0]:.2f}, {nc['eps'][1]:.2f}]" if nc["eps"] else "—"
        e1 = f"[{nc['eps_n1'][0]:.2f}, {nc['eps_n1'][1]:.2f}]" if nc.get("eps_n1") else "—"
        print(f"{nc['stock']:<6}{nc['quarter']:<8}[{lo / 1000:>7,.0f},{hi / 1000:>8,.0f}]  {yoy:<8}{e:<14}{e1:<14}")
    logger.info("nowcast 完成 %d 檔，詳情見 analysis/<股號>_nowcast.md", len(rows))
    return 0
