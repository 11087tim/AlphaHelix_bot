"""產生台股去槓桿壓力儀表板（自足式單檔 HTML → docs/leverage.html）。

讀 data/leverage/ 本地庫 + DPI，畫成內嵌 SVG 圖表，無外部相依、深/淺色自適應。
呼叫 build() 產生檔案；供 src.main 的 leverage mode 與 CLI 共用。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

if __package__:
    from .leverage import compute_dpi, dpi_level, load_market
    from .leverage_ingest import NAMES
else:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.leverage import compute_dpi, dpi_level, load_market
    from src.leverage_ingest import NAMES

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "leverage"
OUT = ROOT / "docs" / "leverage.html"


def _load(name):
    return json.loads((DATA / f"{name}.json").read_text())


def _load_names():
    """股號→股名。優先 names.json；退回最新一份 TWTA1U 快取（含名稱）。"""
    p = DATA / "names.json"
    if p.exists():
        return json.loads(p.read_text())
    caches = sorted((DATA / "_twse_cache").glob("*.json"))
    if caches:
        return {row[0]: row[1] for row in json.loads(caches[-1].read_text())}
    return {}


def line_chart(dates, values, w=680, h=220, color="#3b82f6", fill=True,
               reflines=None, y_fmt=lambda v: f"{v:,.0f}"):
    reflines = reflines or []
    pl, pr, pt, pb = 56, 14, 14, 26
    iw, ih = w - pl - pr, h - pt - pb
    lo, hi = min(values), max(values)
    for _, ry, _ in reflines:
        lo, hi = min(lo, ry), max(hi, ry)
    rng = (hi - lo) or 1
    lo -= rng * 0.08
    hi += rng * 0.08
    rng = hi - lo

    def X(i):
        return pl + (i / (len(values) - 1) * iw if len(values) > 1 else 0)

    def Y(v):
        return pt + ih - (v - lo) / rng * ih

    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(values))
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" preserveAspectRatio="xMidYMid meet">']
    for frac in (0, 0.5, 1):
        yv = lo + rng * frac
        y = Y(yv)
        parts.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{w-pr}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{pl-6}" y="{y+3:.1f}" class="axis" text-anchor="end">{y_fmt(yv)}</text>')
    for label, ry, rc in reflines:
        y = Y(ry)
        parts.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{w-pr}" y2="{y:.1f}" '
                     f'stroke="{rc}" stroke-width="1" stroke-dasharray="4 3" opacity="0.7"/>')
        parts.append(f'<text x="{w-pr}" y="{y-4:.1f}" class="refl" fill="{rc}" text-anchor="end">{label}</text>')
    if fill:
        area = f"{pl},{pt+ih} " + pts + f" {X(len(values)-1):.1f},{pt+ih}"
        parts.append(f'<polygon points="{area}" fill="{color}" opacity="0.12"/>')
    parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')
    parts.append(f'<circle cx="{X(len(values)-1):.1f}" cy="{Y(values[-1]):.1f}" r="3.2" fill="{color}"/>')
    parts.append(f'<text x="{pl}" y="{h-6}" class="axis" text-anchor="start">{dates[0][5:]}</text>')
    parts.append(f'<text x="{w-pr}" y="{h-6}" class="axis" text-anchor="end">{dates[-1][5:]}</text>')
    parts.append("</svg>")
    return "".join(parts)


def dpi_chart(rows, w=680, h=220):
    dates = [r["date"] for r in rows]
    vals = [r["dpi"] for r in rows]
    pl, pr, pt, pb = 36, 14, 14, 26
    iw, ih = w - pl - pr, h - pt - pb
    hi = max(60, max(vals) * 1.15)

    def X(i):
        return pl + i / (len(vals) - 1) * iw

    def Y(v):
        return pt + ih - v / hi * ih

    bands = [(0, 15, "#10b98122"), (15, 30, "#f59e0b22"), (30, 50, "#f9731622"), (50, hi, "#ef444422")]
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" preserveAspectRatio="xMidYMid meet">']
    for a, b, c in bands:
        ya, yb = Y(b), Y(a)
        parts.append(f'<rect x="{pl}" y="{ya:.1f}" width="{iw}" height="{yb-ya:.1f}" fill="{c}"/>')
    for gl in (15, 30, 50):
        if gl < hi:
            parts.append(f'<text x="{pl-4}" y="{Y(gl)+3:.1f}" class="axis" text-anchor="end">{gl}</text>')
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
    parts.append(f'<polygon points="{pl},{pt+ih} {pts} {X(len(vals)-1):.1f},{pt+ih}" fill="#6366f1" opacity="0.15"/>')
    parts.append(f'<polyline points="{pts}" fill="none" stroke="#6366f1" stroke-width="2" stroke-linejoin="round"/>')
    parts.append(f'<circle cx="{X(len(vals)-1):.1f}" cy="{Y(vals[-1]):.1f}" r="3.5" fill="#6366f1"/>')
    parts.append(f'<text x="{pl}" y="{h-6}" class="axis" text-anchor="start">{dates[0][5:]}</text>')
    parts.append(f'<text x="{w-pr}" y="{h-6}" class="axis" text-anchor="end">{dates[-1][5:]}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _pct(a, b):
    return (b - a) / a * 100 if a else 0.0


def build() -> Path:
    market = load_market()
    dpi_rows = compute_dpi(market)
    last = dpi_rows[-1]
    lvl, _ = dpi_level(last["dpi"])
    dates = [m["date"] for m in market]
    maint = [m["maint"] for m in market]
    bal_yi = [m["margin_bal"] / 1e8 for m in market]

    bx_market = _load("buxian_market")
    bx_dates = [r["date"] for r in bx_market]
    bx_vals = [r["buxian_total_kshares"] / 1e5 for r in bx_market]  # 仟股→億股

    # 全市場個股快照（最新交易日）：融資餘額/使用率/融券/借券賣出/不限用途 + 回溯期間 Δ%
    names = _load_names()
    watch = set(NAMES)  # 觀察清單標 ★
    mkt_margin = _load("mkt_margin")
    table_date = max(r["d"] for r in mkt_margin)
    mg_by = defaultdict(list)
    for r in mkt_margin:
        mg_by[r["id"]].append(r)
    short_last = {r["id"]: r for r in _load("mkt_short") if r["d"] == table_date}
    bx_last = {r["id"]: r for r in _load("mkt_buxian") if r["d"] == table_date}
    stock_rows = []
    for sid, recs in mg_by.items():
        recs.sort(key=lambda r: r["d"])
        b = recs[-1]
        if b["d"] != table_date:
            continue
        mbal, lim = b["mbal"], b["mlim"]
        use = round(mbal / lim * 100, 1) if lim else 0.0
        chg = round(_pct(recs[0]["mbal"], mbal), 1)
        s, bxr = short_last.get(sid), bx_last.get(sid)
        stock_rows.append([
            sid, names.get(sid, sid), mbal, use,
            s["fin"] if s else 0, s["sbl"] if s else 0,
            bxr["bx"] if bxr else 0, chg, 1 if sid in watch else 0,
        ])
    stock_rows.sort(key=lambda r: -r[2])  # 預設融資餘額由大到小
    n_stocks = len(stock_rows)
    stock_json = json.dumps(stock_rows, ensure_ascii=False, separators=(",", ":"))

    bal_chg = _pct(bal_yi[0], bal_yi[-1])
    maint_chg = maint[-1] - maint[0]
    bx_chg = _pct(bx_vals[0], bx_vals[-1])
    dpi_max = max(r["dpi"] for r in dpi_rows)
    lv_idx = int(last["dpi"] // 15) if last["dpi"] < 60 else 4

    html = f"""<div class="wrap">
  <header>
    <h1>台股去槓桿壓力儀表板</h1>
    <p class="sub">資料日期 <b>{last['date']}</b>｜回溯 {dates[0]} ~ {dates[-1]}（{len(dates)} 個交易日）｜資料源：FinMind、TWSE</p>
  </header>

  <section class="hero">
    <div class="gauge lv-{lv_idx}">
      <div class="dpi-num">{last['dpi']:.0f}</div>
      <div class="dpi-lvl">{lvl}</div>
      <div class="dpi-cap">去槓桿壓力指數 DPI</div>
    </div>
    <div class="hero-txt">
      <p>回溯期間維持率介於 <b>{min(maint):.0f}%–{max(maint):.0f}%</b>（斷頭線 130%），整體融資槓桿壓力
      <b>{lvl}</b>，DPI 期間最高 <b>{dpi_max:.0f}</b>。</p>
      <p class="warn">⚠️ 融資餘額期間 <b class="{'up' if bal_chg>=0 else 'down'}">{bal_chg:+.0f}%</b>（{bal_yi[0]:,.0f}→{bal_yi[-1]:,.0f} 億）——留意槓桿水位是否墊高、靠股價撐住維持率。</p>
    </div>
  </section>

  <section class="cards">
    <div class="card"><div class="k">融資餘額</div><div class="v">{bal_yi[-1]:,.0f}<span>億</span></div><div class="d {'up' if bal_chg>=0 else 'down'}">{bal_chg:+.1f}% / 期間</div></div>
    <div class="card"><div class="k">融資維持率</div><div class="v">{maint[-1]:.1f}<span>%</span></div><div class="d {'up' if maint_chg>=0 else 'down'}">{maint_chg:+.1f}pt / 期間</div></div>
    <div class="card"><div class="k">融券餘額</div><div class="v">{market[-1]['short_shares']:,}<span>張</span></div><div class="d">散戶放空</div></div>
    <div class="card"><div class="k">不限用途擔保品</div><div class="v">{bx_vals[-1]:,.0f}<span>億股</span></div><div class="d {'up' if bx_chg>=0 else 'down'}">{bx_chg:+.1f}% / 期間</div></div>
  </section>

  <section class="grid2">
    <div class="panel"><h2>去槓桿壓力指數 DPI</h2>{dpi_chart(dpi_rows)}
      <p class="note">綠 0–15 低｜黃 15–30 偏低｜橙 30–50 中等｜紅 50+ 偏高。0.55×維持率水位(凸) + 0.30×維持率動能 + 0.15×去槓桿進行中，動能/去槓桿再乘「脆弱度」gate（維持率越接近危險區越放大）。</p>
    </div>
    <div class="panel"><h2>融資維持率 %（越低越接近追繳）</h2>
      {line_chart(dates, maint, color="#f59e0b", reflines=[("警戒 160", 160, "#f97316"), ("斷頭 130", 130, "#ef4444")], y_fmt=lambda v: f"{v:.0f}%")}
      <p class="note">離斷頭線 130% 越遠越安全。</p>
    </div>
    <div class="panel"><h2>大盤融資餘額（億元）</h2>
      {line_chart(dates, bal_yi, color="#3b82f6", y_fmt=lambda v: f"{v:,.0f}")}
      <p class="note">散戶借錢做多的總額。</p>
    </div>
    <div class="panel"><h2>不限用途借款 擔保品（億股）</h2>
      {line_chart(bx_dates, bx_vals, color="#8b5cf6", y_fmt=lambda v: f"{v:,.0f}")}
      <p class="note">散戶拿股票質押借錢（融資看不到的另一條槓桿）。單位仟股彙總，尚未×股價換算元。</p>
    </div>
  </section>

  <section class="panel">
    <h2>個股槓桿結構（全市場，最新 {table_date}）</h2>
    <div class="tctl">
      <input id="levSearch" type="search" placeholder="搜尋代號或名稱…" autocomplete="off">
      <label>顯示 <select id="levCount">
        <option value="1">1</option><option value="5">5</option>
        <option value="25" selected>25</option><option value="50">50</option>
        <option value="100">100</option><option value="0">全部</option>
      </select> 檔</label>
      <span class="tcount" id="levInfo"></span>
    </div>
    <div class="twrap"><table id="levTable">
      <thead><tr>
        <th>股票</th>
        <th class="srt" data-k="2">融資餘額(張)</th>
        <th class="srt" data-k="3">融資使用率</th>
        <th class="srt" data-k="4">融券(張)</th>
        <th class="srt" data-k="5">借券賣出(張)</th>
        <th class="srt" data-k="6">不限用途(仟股)</th>
        <th class="srt" data-k="7">融資Δ%</th>
      </tr></thead>
      <tbody id="levBody"></tbody>
    </table></div>
    <p class="note">共 {n_stocks:,} 檔（融資可交易宇宙）。<span class="star">★</span>＝觀察清單。融資使用率＝餘額/限額（<span class="hot-t">≥40% 紅</span> 散戶擁擠）。空方看「借券賣出（法人）」遠大於「融券（散戶）」。點欄位標題可排序，預設融資餘額由大到小。Δ% 為回溯期間變化。</p>
  </section>

  <footer>AlphaHelix · 台股槓桿監控 · 產生於 {datetime.now():%Y-%m-%d %H:%M}｜僅供研究，非投資建議</footer>
</div>

<style>
.wrap{{max-width:1080px;margin:0 auto;padding:20px 16px 48px;font-family:-apple-system,"Noto Sans TC",system-ui,sans-serif;color:var(--fg)}}
:root{{--fg:#1e293b;--mut:#64748b;--bg2:#ffffff;--bd:#e2e8f0;--panel:#f8fafc}}
@media(prefers-color-scheme:dark){{:root{{--fg:#e2e8f0;--mut:#94a3b8;--bg2:#0f172a;--bd:#1e293b;--panel:#111827}}}}
:root[data-theme="dark"]{{--fg:#e2e8f0;--mut:#94a3b8;--bg2:#0f172a;--bd:#1e293b;--panel:#111827}}
:root[data-theme="light"]{{--fg:#1e293b;--mut:#64748b;--bg2:#ffffff;--bd:#e2e8f0;--panel:#f8fafc}}
header h1{{font-size:1.5rem;margin:0 0 4px}}
.sub{{color:var(--mut);font-size:.85rem;margin:0}}
.hero{{display:flex;gap:20px;align-items:center;margin:20px 0;flex-wrap:wrap}}
.gauge{{flex:0 0 150px;text-align:center;border-radius:16px;padding:18px 10px;background:var(--panel);border:1px solid var(--bd)}}
.dpi-num{{font-size:3rem;font-weight:800;line-height:1}}
.dpi-lvl{{font-weight:700;margin-top:2px}}
.dpi-cap{{color:var(--mut);font-size:.72rem;margin-top:4px}}
.lv-0 .dpi-num,.lv-0 .dpi-lvl{{color:#10b981}} .lv-1 .dpi-num,.lv-1 .dpi-lvl{{color:#f59e0b}}
.lv-2 .dpi-num,.lv-2 .dpi-lvl{{color:#f97316}} .lv-3 .dpi-num,.lv-3 .dpi-lvl{{color:#ef4444}} .lv-4 .dpi-num,.lv-4 .dpi-lvl{{color:#dc2626}}
.hero-txt{{flex:1;min-width:260px;font-size:.92rem;line-height:1.6}}
.hero-txt p{{margin:0 0 8px}} .warn{{color:var(--mut)}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0}}
.card{{background:var(--panel);border:1px solid var(--bd);border-radius:12px;padding:12px 14px}}
.card .k{{color:var(--mut);font-size:.78rem}} .card .v{{font-size:1.5rem;font-weight:700;margin:3px 0}}
.card .v span{{font-size:.8rem;font-weight:500;color:var(--mut);margin-left:2px}}
.card .d{{font-size:.76rem;color:var(--mut)}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:14px 0}}
.panel{{background:var(--panel);border:1px solid var(--bd);border-radius:14px;padding:14px 16px}}
.panel h2{{font-size:1rem;margin:0 0 10px}}
.chart{{width:100%;height:auto}}
.grid{{stroke:var(--bd);stroke-width:1}} .axis{{fill:var(--mut);font-size:10px}} .refl{{font-size:9px;font-weight:600}}
.note{{color:var(--mut);font-size:.76rem;line-height:1.5;margin:8px 0 0}}
.up{{color:#10b981}} .down{{color:#ef4444}}
.twrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:.86rem}}
th,td{{padding:8px 10px;text-align:right;border-bottom:1px solid var(--bd);white-space:nowrap}}
th{{color:var(--mut);font-weight:600;font-size:.78rem}} th:first-child,td.tk{{text-align:left}}
td.num{{font-variant-numeric:tabular-nums}} td.em{{font-weight:700}}
td.hot{{color:#ef4444;font-weight:700}} td.warm{{color:#f59e0b;font-weight:600}}
.delta{{font-size:.72rem;margin-left:6px}} .hot-t{{color:#ef4444;font-weight:600}}
.tctl{{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:10px}}
.tctl input,.tctl select{{padding:5px 9px;border-radius:8px;border:1px solid var(--bd);background:var(--bg2);color:var(--fg);font-size:.85rem}}
.tctl input{{min-width:180px}} .tctl label{{font-size:.85rem;color:var(--mut)}}
.tcount{{font-size:.8rem;color:var(--mut);margin-left:auto}}
th.srt{{cursor:pointer;user-select:none;white-space:nowrap}} th.srt:hover{{color:var(--fg)}}
.star{{color:#f59e0b;margin-right:3px}}
footer{{color:var(--mut);font-size:.74rem;text-align:center;margin-top:28px}}
@media(max-width:720px){{.cards{{grid-template-columns:repeat(2,1fr)}}.grid2{{grid-template-columns:1fr}}}}
</style>"""

    script = _table_script.replace("__DATA__", stock_json)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = ("<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
           "<meta name='viewport' content='width=device-width,initial-scale=1'>"
           "<title>台股去槓桿壓力儀表板</title></head><body>" + html + script + "</body></html>")
    OUT.write_text(doc, encoding="utf-8")
    return OUT


# 全市場個股表：搜尋 + 顯示筆數 + 點欄位排序（純 JS，資料由 __DATA__ 注入）
_table_script = """<script>
(function(){
  const D = __DATA__;
  let sortK = 2, dir = -1, count = 25, q = "";
  const body = document.getElementById("levBody"), info = document.getElementById("levInfo");
  const fmt = n => (n||0).toLocaleString("en-US");
  const pctTxt = v => (v>=0?"+":"") + v + "%";
  function render(){
    let rows = D;
    if(q){ const s=q.toLowerCase(); rows = rows.filter(r => r[0].toLowerCase().includes(s) || String(r[1]).toLowerCase().includes(s)); }
    rows = rows.slice().sort((a,b)=> (a[sortK]<b[sortK]?-1:a[sortK]>b[sortK]?1:0)*dir);
    const shown = count>0 ? rows.slice(0,count) : rows;
    body.innerHTML = shown.map(r=>{
      const useCls = r[3]>=40?"hot":(r[3]>=20?"warm":"");
      const dCls = r[7]>=0?"up":"down";
      const star = r[8] ? '<span class="star">★</span>' : '';
      return '<tr><td class="tk">'+star+'<b>'+r[0]+'</b> '+r[1]+'</td>'
        + '<td class="num">'+fmt(r[2])+'</td>'
        + '<td class="num '+useCls+'">'+r[3]+'%</td>'
        + '<td class="num">'+fmt(r[4])+'</td>'
        + '<td class="num em">'+fmt(r[5])+'</td>'
        + '<td class="num">'+fmt(r[6])+'</td>'
        + '<td class="num '+dCls+'">'+pctTxt(r[7])+'</td></tr>';
    }).join("");
    info.textContent = "顯示 " + shown.length + " / " + rows.length + " 檔";
  }
  document.getElementById("levSearch").addEventListener("input", e=>{ q=e.target.value.trim(); render(); });
  document.getElementById("levCount").addEventListener("change", e=>{ count=+e.target.value; render(); });
  document.querySelectorAll("#levTable th.srt").forEach(th=>{
    th.addEventListener("click", ()=>{ const k=+th.dataset.k; if(k===sortK) dir=-dir; else { sortK=k; dir=-1; } render(); });
  });
  render();
})();
</script>"""


if __name__ == "__main__":
    p = build()
    print(f"✅ 產生 {p}")
