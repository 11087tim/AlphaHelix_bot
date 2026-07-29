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
        """歷史「第 q 季 → 次季」營收比值（近 years_back 年）。"""
        fs = []
        for y in range(cy - years_back, cy + 1):
            a, b = _q_sum(rev, y, q), _q_sum(rev, *_next_q(y, q))
            if a and b:
                fs.append(b / a)
        return fs

    # T+1：歷史 QoQ 季節因子
    ny, nq = _next_q(cy, cq)
    f1 = _qoq_factors(cq)
    nxt = (cur_lo * min(f1), cur_hi * max(f1)) if f1 else None
    # T+2：季節因子鏈（在 T+1 區間上再乘一段，區間更寬 → 低信心、方向參考）
    n2y, n2q = _next_q(ny, nq)
    f2 = _qoq_factors(nq)
    nxt2 = (nxt[0] * min(f2), nxt[1] * max(f2)) if nxt and f2 else None

    # 同期比較基準
    yoy_base = _q_sum(rev, cy - 1, cq)
    return {"stock": stock, "quarter": f"{cy}Q{cq}", "known_months": known_months,
            "known_sum": known_sum, "cur": (cur_lo, cur_hi), "yoy_base": yoy_base,
            "next_quarter": f"{ny}Q{nq}", "next": nxt,
            "t2_quarter": f"{n2y}Q{n2q}", "t2": nxt2,
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


def _margin_range(data_dir: Path, stock: str) -> tuple[float, float] | None:
    """cl 事實卡 JSON 近 4 季正值毛利率區間（小數）。"""
    files = sorted((data_dir / "analysis").glob(f"{stock}_clj_*.json"))[-4:]
    gms = []
    for p in files:
        try:
            g = json.loads(p.read_text(encoding="utf-8")).get("gross_margin_pct")
            if g and g > 0:
                gms.append(g / 100)
        except (json.JSONDecodeError, OSError):
            continue
    return (min(gms), max(gms)) if gms else None


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
        shares_info = finmind_client.latest_shares(s, token)
        margin = _margin_range(cfg.data_dir, s)
        cl_sig = _cl_signal(cfg.data_dir, s)

        def _eps(rng):
            if not (rng and shares_info and margin):
                return None
            return (rng[0] * margin[0] * 0.8 * 1000 / shares_info["shares"],
                    rng[1] * margin[1] * 0.8 * 1000 / shares_info["shares"])

        eps_cur, eps_n1, eps_n2 = _eps(nc["cur"]), _eps(nc["next"]), _eps(nc["t2"])
        nc.update({"margin": margin, "eps": eps_cur, "eps_n1": eps_n1})
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
                 "| T+3 | 不出數 | — | — | 無結構性依據（RPO/擴產時程）不預估 |", ""]
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
        if eps_cur:
            lines.append(f"\nEPS 公式：營收 × 毛利率 [{margin[0]:.1%}, {margin[1]:.1%}]（cl 事實卡近 4 季正值）"
                         f"× 0.8(稅) ÷ {shares_info['shares']:,} 股（FinMind {shares_info['date']} 股本）。"
                         "未扣隨營收增加的營業費用，屬上緣估計。")
        else:
            lines.append("\nEPS：無 cl 事實卡毛利率或股本資料，僅出營收。")
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
