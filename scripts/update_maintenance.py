#!/usr/bin/env python3
"""把 TEJ 融資維持率寬表 CSV 轉成管線用的兩個 JSON（快照＋歷史）。

輸入：TEJ 匯出的寬表 CSV（列=日期、欄=股票代號、值=維持率%），預設 ~/Downloads/fin_mainrate.csv
輸出：data/leverage/mkt_maintenance.json（最新日快照，dashboard 維持率/距追繳/LLM 短評用）
      data/leverage/tej_hist.json（近半年逐日歷史，距追繳趨勢圖用）
用法：python scripts/update_maintenance.py [CSV路徑]
更新後記得把兩個 JSON scp 到 VM 並重跑 leverage mode。
"""
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTDIR = ROOT / "data" / "leverage"
HIST_START = "2026-01-20"   # 歷史 JSON 起點（與全市場庫對齊）


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Downloads" / "fin_mainrate.csv"
    with open(src, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        sids = [c.strip() for c in header[1:]]
        dates, rows = [], []
        for row in reader:
            d = row[0].strip()[:10]
            if not d:
                continue
            dates.append(d)
            rows.append(row[1:])

    # 快照：最新一列
    last_i = len(dates) - 1
    ratio = {}
    for j, sid in enumerate(sids):
        v = rows[last_i][j].strip() if j < len(rows[last_i]) else ""
        if v:
            try:
                ratio[sid] = round(float(v), 1)
            except ValueError:
                pass
    # 合併：新 CSV 優先；CSV 宇宙外的個股（ETF/新上市等）沿用既有快照值，不丟資料
    old_p = OUTDIR / "mkt_maintenance.json"
    carried = 0
    if old_p.exists():
        old_ratio = json.loads(old_p.read_text()).get("ratio", {})
        for sid, v in old_ratio.items():
            if sid not in ratio:
                ratio[sid] = v
                carried += 1
    snap = {"date": dates[last_i], "source": "TEJ",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "ratio": ratio}
    old_p.write_text(
        json.dumps(snap, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # 歷史：HIST_START 起
    keep = [i for i, d in enumerate(dates) if d >= HIST_START]
    hdates = [dates[i] for i in keep]
    stocks = {}
    for j, sid in enumerate(sids):
        arr = []
        any_v = False
        for i in keep:
            v = rows[i][j].strip() if j < len(rows[i]) else ""
            if v:
                try:
                    arr.append(round(float(v), 1))
                    any_v = True
                    continue
                except ValueError:
                    pass
            arr.append(None)
        if any_v:
            stocks[sid] = arr
    # 歷史同樣合併：CSV 未涵蓋的個股沿用既有序列（日期軸以新檔為準、對齊補 None）
    hist_p = OUTDIR / "tej_hist.json"
    if hist_p.exists():
        old_h = json.loads(hist_p.read_text())
        omap = {d: i for i, d in enumerate(old_h.get("dates", []))}
        for sid, arr in old_h.get("stocks", {}).items():
            if sid in stocks:
                continue
            stocks[sid] = [arr[omap[d]] if d in omap else None for d in hdates]
    hist = {"dates": hdates, "stocks": stocks}
    hist_p.write_text(
        json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"✅ 快照 {snap['date']}（{len(ratio)} 檔，其中 {carried} 檔沿用舊值）→ mkt_maintenance.json")
    print(f"✅ 歷史 {hdates[0]} ~ {hdates[-1]}（{len(hdates)} 天 × {len(stocks)} 檔）→ tej_hist.json")


if __name__ == "__main__":
    main()
