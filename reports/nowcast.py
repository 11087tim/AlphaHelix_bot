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

    # 次季估計：歷史 QoQ 季節因子
    ny, nq = _next_q(cy, cq)
    factors = []
    for y in range(cy - years_back, cy):
        a, b = _q_sum(rev, y, cq), _q_sum(rev, *_next_q(y, cq))
        if a and b:
            factors.append(b / a)
    nxt = (cur_lo * min(factors), cur_hi * max(factors)) if factors else None

    # 同期比較基準
    yoy_base = _q_sum(rev, cy - 1, cq)
    return {"stock": stock, "quarter": f"{cy}Q{cq}", "known_months": known_months,
            "known_sum": known_sum, "cur": (cur_lo, cur_hi), "yoy_base": yoy_base,
            "next_quarter": f"{ny}Q{nq}", "next": nxt,
            "latest_month": f"{last_y}/{last_m:02d}"}


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
        eps = None
        if shares_info and margin:
            eps = (nc["cur"][0] * margin[0] * 0.8 * 1000 / shares_info["shares"],
                   nc["cur"][1] * margin[1] * 0.8 * 1000 / shares_info["shares"])
        nc.update({"margin": margin, "eps": eps})
        rows.append(nc)

        lo, hi = nc["cur"]
        yoy = f"{(lo + hi) / 2 / nc['yoy_base'] - 1:+.0%}" if nc["yoy_base"] else "—"
        lines = [f"# {s} 月營收 Nowcast（至 {nc['latest_month']}）", "",
                 f"- **{nc['quarter']} 營收估計：[{lo:,.0f}, {hi:,.0f}] 仟元**"
                 f"（已公布 {len(nc['known_months'])}/3 月合計 {nc['known_sum']:,.0f}；YoY 約 {yoy}）"]
        if nc["next"]:
            lines.append(f"- {nc['next_quarter']} 營收估計：[{nc['next'][0]:,.0f}, {nc['next'][1]:,.0f}] 仟元"
                         f"（歷史 QoQ 季節因子外推，不含新訂單/事件調整）")
        if eps:
            lines.append(f"- {nc['quarter']} EPS 基線：**[{eps[0]:.2f}, {eps[1]:.2f}] 元**"
                         f"（毛利率 [{margin[0]:.1%}, {margin[1]:.1%}] × 0.8 稅 ÷ 股本，未扣費用增量，屬上緣）")
        else:
            lines.append("- EPS 基線：無 cl 事實卡毛利率或股本資料，僅出營收估計")
        lines += ["", "方法：已公布月份 ÷ 近 3 年同季月份佔比；區間為歷史佔比極值。純程式計算，無 LLM。"]
        out = cfg.data_dir / "analysis" / f"{s}_nowcast.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines), encoding="utf-8")

    if not rows:
        logger.error("沒有任何股票產出 nowcast。")
        return 1
    print(f"{'股號':<6}{'季度':<8}{'已公布':<5}{'當季營收估計(百萬)':<24}{'YoY':<8}{'EPS基線':<14}")
    for nc in rows:
        lo, hi = nc["cur"]
        yoy = f"{(lo + hi) / 2 / nc['yoy_base'] - 1:+.0%}" if nc["yoy_base"] else "—"
        eps_s = f"[{nc['eps'][0]:.2f}, {nc['eps'][1]:.2f}]" if nc["eps"] else "—"
        print(f"{nc['stock']:<6}{nc['quarter']:<8}{len(nc['known_months'])}/3  "
              f"[{lo / 1000:>8,.0f}, {hi / 1000:>8,.0f}]      {yoy:<8}{eps_s:<14}")
    logger.info("nowcast 完成 %d 檔，詳情見 analysis/<股號>_nowcast.md", len(rows))
    return 0
