#!/usr/bin/env python3
"""把 TEJ 融資維持率寬表 CSV 轉成管線用的兩個 JSON（快照＋歷史）。

輸入（二擇一，依副檔名自動判斷）：
  .csv  ＝全量寬表（列=日期、欄=股票代號）→ 重建快照+歷史（宇宙以該檔為準）
  .xlsx ＝單日/增量寬表（TEJ 日常匯出）→ 合併進既有歷史並更新快照（需 pandas，用 /opt/anaconda3/bin/python 執行）
預設 ~/Downloads/fin_mainrate.csv
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


def merge_daily_xlsx(src: Path):
    """把單日（或多日）寬表 xlsx 合併進既有快照與歷史。"""
    import pandas as pd

    df = pd.read_excel(src)
    hist = json.loads((OUTDIR / "tej_hist.json").read_text())
    snap = json.loads((OUTDIR / "mkt_maintenance.json").read_text())
    dates, stocks = hist["dates"], hist["stocks"]
    merged_days = []
    for _, row in df.iterrows():
        d = str(row.iloc[0])[:10]
        vals = {}
        for c in df.columns[1:]:
            v = row[c]
            if pd.notna(v):
                try:
                    vals[str(c).strip()] = round(float(v), 1)
                except (TypeError, ValueError):
                    pass
        if not vals or d < HIST_START:
            continue
        if d not in dates:
            dates.append(d)
            dates.sort()
            idx = dates.index(d)
            for arr in stocks.values():
                arr.insert(idx, None)
        else:
            idx = dates.index(d)
        for sid, v in vals.items():
            if sid not in stocks:
                stocks[sid] = [None] * len(dates)
            stocks[sid][idx] = v
        if d >= snap.get("date", ""):
            snap = {"date": d, "source": "TEJ",
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "ratio": vals}
        merged_days.append((d, len(vals)))
    (OUTDIR / "tej_hist.json").write_text(
        json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (OUTDIR / "mkt_maintenance.json").write_text(
        json.dumps(snap, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for d, n in merged_days:
        print(f"✅ 合併 {d}（{n} 檔）")
    print(f"✅ 快照 {snap['date']}｜歷史 {dates[0]} ~ {dates[-1]}（{len(dates)} 天）")


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Downloads" / "fin_mainrate.csv"
    if src.suffix.lower() in (".xlsx", ".xls"):
        merge_daily_xlsx(src)
        return
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
    # 宇宙以新 CSV 為準：CSV 未涵蓋者（ETF/特別股等）不帶維持率（表格顯示「—」）
    snap = {"date": dates[last_i], "source": "TEJ",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "ratio": ratio}
    (OUTDIR / "mkt_maintenance.json").write_text(
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
    hist = {"dates": hdates, "stocks": stocks}
    (OUTDIR / "tej_hist.json").write_text(
        json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"✅ 快照 {snap['date']}（{len(ratio)} 檔）→ mkt_maintenance.json")
    print(f"✅ 歷史 {hdates[0]} ~ {hdates[-1]}（{len(hdates)} 天 × {len(stocks)} 檔）→ tej_hist.json")


if __name__ == "__main__":
    main()
