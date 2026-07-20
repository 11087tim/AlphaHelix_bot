"""台股去槓桿壓力指標（DPI）計算引擎。

讀 data/leverage/ 的本地歷史庫，算每日「去槓桿壓力指數」DPI（0–100）。
DPI = 0.55·維持率水位 + 0.30·維持率動能 + 0.15·去槓桿進行中
三個子分數各自 0–100，數字越高＝散戶被迫去槓桿（追繳/斷頭/殺融資）壓力越大。

刻意設計成「凸函數 + 脆弱度 gate」：台股斷頭線 130%、危險區其實是 <160%。
維持率 172%↑ 幾乎不痛，跌破 160% 才快速升溫（凸函數）。
「動能」與「去槓桿進行中」會被脆弱度 gate 壓抑——維持率很高（>186%）時的漲跌
幾乎不算數，只有接近危險區時的快速下墜才會即時亮燈，避免安全期的雜訊誤報。
（融資餘額水位／百分位改當『旁證』另外顯示，不折進 DPI 主分數。）
"""
from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "leverage"

# DPI 權重（動能與去槓桿會再乘上脆弱度 gate）
W_MAINT, W_MOMO, W_UNWIND = 0.55, 0.30, 0.15


def _clip(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def load_market():
    """回傳依日期排序的大盤時間序列：[{date, maint, margin_bal(元), short_shares}]。"""
    mm = {r["date"]: r.get("TotalExchangeMarginMaintenance")
          for r in json.loads((DATA / "market_maintenance.json").read_text())}
    bal, shrt = {}, {}
    for r in json.loads((DATA / "market_margin.json").read_text()):
        if r["name"] == "MarginPurchaseMoney":
            bal[r["date"]] = r["TodayBalance"]          # 融資餘額（元）
        elif r["name"] == "ShortSale":
            shrt[r["date"]] = r["TodayBalance"]         # 融券餘額（張）
    dates = sorted(set(mm) & set(bal))
    out = []
    for d in dates:
        try:
            maint = float(mm[d])
        except (TypeError, ValueError):
            continue
        out.append({"date": d, "maint": maint,
                    "margin_bal": bal[d], "short_shares": shrt.get(d)})
    return out


def _p_maint(m):
    """維持率水位（凸）：172%→0，160%→~28，150%→~62，145%→~80，140%↓→100。"""
    x = _clip(172.0 - m, 0.0, 32.0)
    return 100.0 * (x / 32.0) ** 1.3


def _vuln(m):
    """脆弱度 0–1：維持率 186%↑→0（安全，動能不算數），150%↓→1（貼近危險）。"""
    return _clip((186.0 - m) / (186.0 - 148.0), 0.0, 1.0)


def _gate(m):
    """動能/去槓桿的權重折扣：安全期保留 15% 底、貼近危險時放大到 100%。"""
    return 0.15 + 0.85 * _vuln(m)


def _p_momo(series, i, window=5):
    """維持率動能：近 window 日維持率變化，每跌 10 個百分點 →100。"""
    j = max(0, i - window)
    if j == i:
        return 0.0
    delta = series[i]["maint"] - series[j]["maint"]
    return _clip(-delta / 10.0 * 100.0)


def _p_fuel(series, i):
    """融資水位：融資餘額在已知歷史中的百分位（越高＝可去槓桿的燃料越多）。"""
    hist = [s["margin_bal"] for s in series[: i + 1]]
    cur = series[i]["margin_bal"]
    rank = sum(1 for v in hist if v <= cur) / len(hist)
    return rank * 100.0


def _p_unwind(series, i, window=3):
    """去槓桿進行中：近 window 日融資餘額%變化，每跌 2% →100。"""
    j = max(0, i - window)
    if j == i:
        return 0.0
    base = series[j]["margin_bal"]
    if not base:
        return 0.0
    pct = (series[i]["margin_bal"] - base) / base * 100.0
    return _clip(-pct / 2.0 * 100.0)


def compute_dpi(series=None):
    """回傳每日 DPI 序列（含各子分數與原始值），依日期排序。"""
    if series is None:
        series = load_market()
    out = []
    for i, s in enumerate(series):
        g = _gate(s["maint"])
        pm = _p_maint(s["maint"])
        pmo, pu = _p_momo(series, i), _p_unwind(series, i)
        pf = _p_fuel(series, i)  # 旁證：融資餘額百分位（不進 DPI）
        dpi = W_MAINT * pm + W_MOMO * pmo * g + W_UNWIND * pu * g
        out.append({
            "date": s["date"], "dpi": round(dpi, 1),
            "p_maint": round(pm, 1), "p_momo": round(pmo, 1), "p_unwind": round(pu, 1),
            "gate": round(g, 2), "fuel_pct": round(pf, 1),
            "maint": s["maint"], "margin_bal_yi": round(s["margin_bal"] / 1e8, 1),
            "short_shares": s["short_shares"],
        })
    return out


def dpi_level(dpi):
    """分數分級（給 dashboard 標色/文字）。"""
    if dpi >= 70:
        return ("極高", "紅")
    if dpi >= 50:
        return ("偏高", "橙")
    if dpi >= 30:
        return ("中等", "黃")
    if dpi >= 15:
        return ("偏低", "淺綠")
    return ("低", "綠")


if __name__ == "__main__":
    rows = compute_dpi()
    print("日期          DPI  分級    維持率水位 動能  去槓桿 gate  |  維持率  融資餘額(億) 燃料%")
    for r in rows:
        lvl, _ = dpi_level(r["dpi"])
        print("%-12s %5.1f %-6s %8.1f %6.1f %6.1f %5.2f  |  %6.2f  %8.1f %6.1f" % (
            r["date"], r["dpi"], lvl, r["p_maint"], r["p_momo"], r["p_unwind"],
            r["gate"], r["maint"], r["margin_bal_yi"], r["fuel_pct"]))
