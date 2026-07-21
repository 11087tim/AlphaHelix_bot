"""產生台股去槓桿壓力儀表板（自足式單檔 HTML → docs/leverage.html）。

讀 data/leverage/ 本地庫，畫成內嵌 SVG 圖表，無外部相依、深/淺色自適應。
呼叫 build() 產生檔案；供 src.main 的 leverage mode 與 CLI 共用。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

if __package__:
    from .leverage import load_market
    from .leverage_ingest import NAMES
else:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.leverage import load_market
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
               reflines=None, y_fmt=lambda v: f"{v:,.0f}", val_fmt=None):
    reflines = reflines or []
    val_fmt = val_fmt or y_fmt
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
    # hover 用資料點：[x, y, 日期, 格式化數值]
    hover_pts = json.dumps(
        [[round(X(i), 1), round(Y(v), 1), dates[i], val_fmt(v)] for i, v in enumerate(values)],
        ensure_ascii=False, separators=(",", ":"))
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" preserveAspectRatio="xMidYMid meet" '
             f"data-pts='{hover_pts}'>"]
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
    # hover 十字線與焦點（JS 控制）
    parts.append(f'<line class="xh" x1="0" x2="0" y1="{pt}" y2="{pt+ih}" stroke="var(--mut)" '
                 f'stroke-width="1" stroke-dasharray="3 3" style="display:none"/>')
    parts.append(f'<circle class="fc" r="4" fill="{color}" stroke="var(--bg2)" stroke-width="1.5" style="display:none"/>')
    parts.append("</svg>")
    return "".join(parts)


def _pct(a, b):
    return (b - a) / a * 100 if a else 0.0


def build_hist() -> Path:
    """產出 docs/leverage_hist.json：個股歷史序列（趨勢圖用，前端按需 fetch）。
    b=融資餘額張(近252交易日) m=TEJ維持率(近126日,靜態匯出)。"""
    mg = _load("mkt_margin")
    md = sorted({r["d"] for r in mg})[-252:]
    mdi = {d: i for i, d in enumerate(md)}
    pdl = sorted({r["d"] for r in _load("mkt_price")})[-126:]  # 距追繳(m) 的日期軸
    pdi = {d: i for i, d in enumerate(pdl)}
    tejp = DATA / "tej_hist.json"
    tej = json.loads(tejp.read_text()) if tejp.exists() else {"dates": [], "stocks": {}}

    stocks = {}
    for r in mg:
        i = mdi.get(r["d"])
        if i is None:
            continue
        o = stocks.setdefault(r["id"], {"b": [None] * len(md), "m": [None] * len(pdl)})
        o["b"][i] = r["mbal"]
    for sid, arr in tej.get("stocks", {}).items():
        o = stocks.get(sid)
        if not o:
            continue
        for j, d in enumerate(tej["dates"]):
            i = pdi.get(d)
            if i is not None and arr[j] is not None:
                o["m"][i] = arr[j]
    out = {"pd": pdl, "md": md, "s": stocks}
    path = OUT.parent / "leverage_hist.json"
    path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    return path


def _liq_wave(call=130.0):
    """斷頭潮時間表：每日「跌破130存量」與「推定處分流量」（首次跌破+3營業日；
    補繳期內反彈者不計；部位以該日（或最新）融資餘額×收盤精算）。
    回傳 (rows, peak_date)；rows=[{date, st_n, st_v, fl_n, fl_v, future}]。"""
    from datetime import date as _date, timedelta as _td

    thp = DATA / "tej_hist.json"
    if not thp.exists():
        return [], None
    h = json.loads(thp.read_text())
    hd, S = h["dates"], h["stocks"]
    li = len(hd) - 1

    mbal_hist, px_hist = defaultdict(dict), defaultdict(dict)
    for r in _load("mkt_margin"):
        mbal_hist[r["id"]][r["d"]] = r["mbal"]
    for r in _load("mkt_price"):
        if r.get("c"):
            px_hist[r["id"]][r["d"]] = r["c"]

    def _posval(sid, d):
        mb = mbal_hist.get(sid, {})
        px = px_hist.get(sid, {})
        db = max((k for k in mb if k <= d), default=None)
        dp = max((k for k in px if k <= d), default=None)
        if not db or not dp:
            return 0.0
        return mb[db] * 1000 * px[dp] / 1e8

    def _add_bd(dstr, n):
        d = _date.fromisoformat(dstr)
        c = 0
        while c < n:
            d += _td(days=1)
            if d.weekday() < 5:
                c += 1
        return d.isoformat()

    # 存量：每日跌破130家數/部位
    stock_daily = {}
    for i in range(max(0, li - 9), li + 1):
        c, v = 0, 0.0
        for sid, arr in S.items():
            if arr[i] is not None and arr[i] < call:
                c += 1
                v += _posval(sid, hd[i])
        stock_daily[hd[i]] = (c, v)

    # 流量：每檔的「跌破 run」→ 若補繳期(D1,D2)未反彈 → D3 計一次處分
    flow = defaultdict(lambda: [0, 0.0])
    for sid, arr in S.items():
        i = 0
        while i <= li:
            if arr[i] is None or arr[i] >= call:
                i += 1
                continue
            j = i  # run: [i..j]
            while j + 1 <= li and arr[j + 1] is not None and arr[j + 1] < call:
                j += 1
            # 補繳期內（i+1, i+2）反彈者不計；資料未及(未來)視為未反彈（估計）
            survived = all(arr[k] is not None and arr[k] < call
                           for k in range(i + 1, min(i + 3, li + 1)))
            if survived:
                d3 = hd[i + 3] if i + 3 <= li else _add_bd(hd[li], i + 3 - li)
                e = flow[d3]
                e[0] += 1
                e[1] += _posval(sid, min(d3, hd[li]))
            i = j + 1

    dates_out = hd[-8:]
    fut = [d for d in sorted(flow) if d > hd[li]][:4]
    rows = []
    for d in dates_out + fut:
        st = stock_daily.get(d)
        fl = flow.get(d, [0, 0.0])
        rows.append({"date": d, "st_n": st[0] if st else None,
                     "st_v": round(st[1]) if st else None,
                     "fl_n": fl[0], "fl_v": round(fl[1]), "future": d > hd[li]})
    peak = max(rows, key=lambda r: r["fl_v"])["date"] if rows else None
    return rows, peak


def build() -> Path:
    market = load_market()
    last_date = market[-1]["date"]
    dates = [m["date"] for m in market]
    maint = [m["maint"] for m in market]
    bal_yi = [m["margin_bal"] / 1e8 for m in market]

    bx_market = _load("buxian_market")
    bx_dates = [r["date"] for r in bx_market]
    bx_vals = [r["buxian_total_kshares"] / 1e5 for r in bx_market]  # 仟股→億股

    # 全市場個股快照（最新交易日）
    names = _load_names()
    watch = set(NAMES)  # 觀察清單標 ★
    mkt_margin = _load("mkt_margin")
    table_date = max(r["d"] for r in mkt_margin)
    mg_by = defaultdict(list)
    for r in mkt_margin:
        mg_by[r["id"]].append(r)
    # 市值比重（分母＝全市場總市值）
    mv_rows = _load("mkt_mktval") if (DATA / "mkt_mktval.json").exists() else []
    mv_date = max((r["d"] for r in mv_rows), default=None)
    mv_by = {r["id"]: r["mv"] for r in mv_rows if r["d"] == mv_date}
    mv_total = sum(mv_by.values()) or 1
    # 融資維持率（TEJ 實際）→ 距追繳
    mp = DATA / "mkt_maintenance.json"
    maint_ratio = json.loads(mp.read_text()).get("ratio", {}) if mp.exists() else {}
    # 股價歷史（近5日跌幅 + 融資佔市值用現價）
    price_hist = defaultdict(list)
    for r in _load("mkt_price"):
        if r.get("c"):
            price_hist[r["id"]].append((r["d"], r["c"]))
    # 大盤相對值：融資餘額(元) / 全市場總市值
    mkt_margin_ratio = market[-1]["margin_bal"] / mv_total * 100 if mv_total > 1 else 0
    CALL = 130.0  # 融資追繳線（供「距追繳」欄）
    # 可能斷頭日：TEJ 歷史找「連續跌破130」起點 → +3個營業日（T+2補繳、第3營業日開盤處分；反彈回130以上暫緩）
    liq = {}
    thp = DATA / "tej_hist.json"
    if thp.exists():
        from datetime import date as _date, timedelta as _td

        def _add_bd(dstr, n):
            d = _date.fromisoformat(dstr)
            c = 0
            while c < n:
                d += _td(days=1)
                if d.weekday() < 5:
                    c += 1
            return d.isoformat()

        tejh = json.loads(thp.read_text())
        hd = tejh["dates"]
        li = len(hd) - 1
        for sid, arr in tejh["stocks"].items():
            if arr[li] is None or arr[li] >= CALL:
                continue
            j = li
            while j > 0 and arr[j - 1] is not None and arr[j - 1] < CALL:
                j -= 1
            liq[sid] = _add_bd(hd[j], 3)
    # 欄位精簡版（易燃×火苗）：[代號, 名稱, 融資餘額張, 市值比重, 融資佔市值, 近5日跌幅, 距追繳, ★]
    stock_rows = []
    for sid, recs in mg_by.items():
        recs.sort(key=lambda r: r["d"])
        b = recs[-1]
        if b["d"] != table_date:
            continue
        mbal = b["mbal"]
        weight = round(mv_by.get(sid, 0) / mv_total * 100, 3)
        M = maint_ratio.get(sid, 0)
        dist = round((M - CALL) / M * 100, 1) if M > 0 else 9999  # 距追繳%（負=已破；9999=無資料）
        mv = mv_by.get(sid, 0)
        ph = sorted(price_hist.get(sid, []))
        px = ph[-1][1] if ph else 0
        mratio = round(mbal * 1000 * px / mv * 100, 2) if mv and px else None  # 融資佔市值%
        chg5 = round((ph[-1][1] / ph[-6][1] - 1) * 100, 1) if len(ph) >= 6 else None  # 近5交易日漲跌%
        hist = [r["mbal"] for r in recs[-252:]]  # 融資餘額近52週（252交易日）
        rank52 = round(sum(1 for v in hist if v <= mbal) / len(hist) * 100) if len(hist) >= 30 else None
        stock_rows.append([sid, names.get(sid, sid), mbal, weight, mratio, chg5, dist,
                           1 if sid in watch else 0, rank52, liq.get(sid)])
    stock_rows.sort(key=lambda r: -r[2])  # 預設融資餘額由大到小
    n_stocks = len(stock_rows)
    stock_json = json.dumps(stock_rows, ensure_ascii=False, separators=(",", ":"))

    # 斷頭潮時間表
    wave_rows, wave_peak = _liq_wave(CALL)
    wave_html = ""
    if wave_rows:
        wr = []
        for r in wave_rows:
            peak_cls = ' class="peak"' if r["date"] == wave_peak and r["fl_v"] > 0 else ""
            status = "未來(估)" if r["future"] else ("資料日" if r["date"] == wave_rows[-1]["date"] and not r["future"] else "")
            st_n = "—" if r["st_n"] is None else f'{r["st_n"]:,}'
            st_v = "—" if r["st_v"] is None else f'{r["st_v"]:,}'
            fl_cls = "hot" if r["fl_v"] >= 500 else ("warm" if r["fl_v"] >= 100 else "")
            wr.append(f'<tr{peak_cls}><td class="tk">{r["date"]}{"（未來,估）" if r["future"] else ""}</td>'
                      f'<td class="num">{st_n}</td><td class="num">{st_v}</td>'
                      f'<td class="num">{r["fl_n"]:,}</td>'
                      f'<td class="num {fl_cls}">{r["fl_v"]:,}</td></tr>')
        wave_html = f"""  <section class="panel">
    <h2>斷頭潮時間表（估計）</h2>
    <div class="twrap"><table class="mini">
      <thead><tr><th>日期</th><th>跌破130家數(存量)</th><th>其融資部位(億)</th><th>推定處分檔數(流量)</th><th>推定處分部位(億)</th></tr></thead>
      <tbody>{''.join(wr)}</tbody>
    </table></div>
    <p class="note">存量＝當日維持率跌破130%的個股與其融資部位市值（該日餘額×收盤精算）。流量＝依「首次跌破＋3營業日開盤處分」推定當日被處分的部位；補繳期（跌破後兩個營業日）內維持率反彈回130以上者不計；未來日假設不反彈、以最新部位估計，未計國定假日。<span class="hot-t">紅框列＝推定高峰</span>。實際賣壓依各戶補繳情況而定，僅供研究。</p>
  </section>

"""

    # LLM 去槓桿壓力短評（由 leverage_comment 產生；無檔案則不顯示）
    llm_html = ""
    cp = DATA / "llm_comment.json"
    if cp.exists():
        try:
            cm = json.loads(cp.read_text())
            rows_h = []
            for i, pk in enumerate(cm.get("picks", []), 1):
                d = pk.get("dist")
                d_txt = "—" if d is None else ("已追繳" if d < 0 else f"跌{d:.1f}%")
                d_cls = "hot" if (d is not None and d < 0) else ("warm" if (d is not None and d < 5) else "")
                c5 = pk.get("chg5", 0)
                c5_cls = "hot" if c5 <= -10 else ("warm" if c5 <= -5 else "")
                rows_h.append(
                    f'<tr><td class="num">{i}</td>'
                    f'<td class="tk"><b>{pk["id"]}</b> {pk["name"]}</td>'
                    f'<td class="num">{pk.get("weight", 0):.2f}%</td>'
                    f'<td class="num {c5_cls}">{c5:+.1f}%</td>'
                    f'<td class="num {d_cls}">{d_txt}</td>'
                    f'<td class="reason">{pk.get("reason", "")}</td></tr>')
            llm_html = f"""  <section class="panel">
    <h2>🤖 去槓桿壓力短評（LLM 依數據挑選，大市值優先）</h2>
    <p class="llm-sum">{cm.get("summary", "")}</p>
    <div class="twrap"><table class="mini">
      <thead><tr><th>#</th><th>股票</th><th>市值比重</th><th>近5日</th><th>距追繳</th><th>依據</th></tr></thead>
      <tbody>{''.join(rows_h)}</tbody>
    </table></div>
    <p class="note">由 {cm.get("model", "LLM")} 於 {cm.get("generated_at", "")} 依 {cm.get("date", "")} 收盤數據自動挑選（最多 20 檔、僅供研究非投資建議）；理由僅引用表列數據。</p>
  </section>

"""
        except Exception:
            llm_html = ""


    html = f"""<div class="wrap">
  <header>
    <h1>台股去槓桿壓力儀表板</h1>
    <p class="sub">資料日期 <b>{last_date}</b>｜回溯 {dates[0]} ~ {dates[-1]}（{len(dates)} 個交易日）｜資料源：FinMind、TWSE</p>
  </header>

  <section class="cards">
    <div class="card"><div class="k">融資餘額</div><div class="v">{bal_yi[-1]:,.0f}<span>億</span></div><div class="d">佔大盤市值 {mkt_margin_ratio:.2f}%</div></div>
    <div class="card"><div class="k">融資維持率</div><div class="v">{maint[-1]:.1f}<span>%</span></div><div class="d">追繳線 130%</div></div>
    <div class="card"><div class="k">融券餘額</div><div class="v">{market[-1]['short_shares']:,}<span>張</span></div><div class="d">散戶放空</div></div>
    <div class="card"><div class="k">不限用途擔保品</div><div class="v">{bx_vals[-1]:,.0f}<span>億股</span></div><div class="d">股票質押借款</div></div>
  </section>

  <section class="grid2">
    <div class="panel"><h2>融資維持率 %（越低越接近追繳）</h2>
      {line_chart(dates, maint, color="#f59e0b", reflines=[("警戒 160", 160, "#f97316"), ("斷頭 130", 130, "#ef4444")], y_fmt=lambda v: f"{v:.0f}%", val_fmt=lambda v: f"{v:.2f}%")}
      <p class="note">離斷頭線 130% 越遠越安全。</p>
    </div>
    <div class="panel"><h2>大盤融資餘額（億元）</h2>
      {line_chart(dates, bal_yi, color="#3b82f6", y_fmt=lambda v: f"{v:,.0f}", val_fmt=lambda v: f"{v:,.1f} 億")}
      <p class="note">散戶借錢做多的總額。</p>
    </div>
    <div class="panel"><h2>不限用途借款 擔保品（億股）</h2>
      {line_chart(bx_dates, bx_vals, color="#8b5cf6", y_fmt=lambda v: f"{v:,.0f}", val_fmt=lambda v: f"{v:,.1f} 億股")}
      <p class="note">散戶拿股票質押借錢（融資看不到的另一條槓桿）。單位仟股彙總，尚未×股價換算元。</p>
    </div>
  </section>

{llm_html}{wave_html}  <section class="panel">
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
        <th class="srt" data-k="3">市值比重</th>
        <th class="srt" data-k="4">融資佔市值</th>
        <th class="srt" data-k="5">近5日跌幅</th>
        <th class="srt" data-k="6">距追繳</th>
        <th class="srt" data-k="9">可能斷頭日</th>
        <th class="srt" data-k="8">融資52週分位</th>
      </tr></thead>
      <tbody id="levBody"></tbody>
    </table></div>
    <p class="note">共 {n_stocks:,} 檔（融資可交易宇宙）。<span class="star">★</span>＝觀察清單。挑欄邏輯＝<b>易燃物×火苗</b>：<b>融資佔市值</b>（易燃物）＝融資部位市值（餘額×現價）/個股總市值，<span class="hot-t">≥8% 紅</span>、≥4% 橙——籌碼中融資越重、跌時賣壓放大越兇；<b>近5日跌幅</b>（火苗）＝近 5 個交易日漲跌，<span class="hot-t">≤−10% 紅</span>、≤−5% 橙——正在燒掉維持率；<b>距追繳</b>（引信）＝（TEJ 實際維持率−130）/維持率，即還能跌多少 % 觸及追繳線（<span class="hot-t">紅＝已追繳</span>、橙&lt;5%），與股價 1:1 連動為精確值。市值比重＝影響力（個股市值/全市場）。<b>融資52週分位</b>＝目前融資餘額在近 52 週（252 交易日）的百分位（<span class="hot-t">≥90 紅</span>、≥70 橙＝融資堆在一年高檔、燃料滿；小字為絕對張數；歷史不足 30 日顯示「—」）。三者同時亮＝隔日斷頭殺盤候選。<b>可能斷頭日</b>＝維持率連續跌破130的起始日＋3個營業日（追繳後 T+2 未補繳、第3營業日開盤處分；期間若反彈回130以上則暫緩，故為估計；未計國定假日）。「處分中」＝推定處分日已到。點欄位標題可排序。</p>
  </section>

  <section class="panel">
    <h2>個股指標趨勢</h2>
    <div class="tctl">
      <input id="trStock" list="trList" placeholder="輸入代號或名稱…" autocomplete="off">
      <datalist id="trList"></datalist>
      <label>指標 <select id="trMetric">
        <option value="bal" selected>融資餘額(張)</option>
        <option value="dist">距追繳</option>
      </select></label>
      <span class="tcount" id="trInfo"></span>
    </div>
    <div id="trChart"><p class="note">輸入股票代號後顯示趨勢。</p></div>
    <p class="note">融資餘額為近 52 週；其餘指標近 6 個月。距追繳依 TEJ 維持率歷史（靜態匯出至最新交易日）。歷史數據首次查詢時載入。</p>
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
.delta{{font-size:.72rem;margin-left:6px}} .sub{{display:block;font-size:.68rem;color:var(--mut)}} .hot-t{{color:#ef4444;font-weight:600}}
.llm-sum{{font-size:.9rem;line-height:1.65;margin:0 0 10px}}
tr.peak td{{border-top:1.5px solid #ef4444;border-bottom:1.5px solid #ef4444;font-weight:600}}
td.reason{{text-align:left;white-space:normal;font-size:.8rem;color:var(--mut);min-width:220px}}
.tctl{{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:10px}}
.tctl input,.tctl select{{padding:5px 9px;border-radius:8px;border:1px solid var(--bd);background:var(--bg2);color:var(--fg);font-size:.85rem}}
.tctl input{{min-width:180px}} .tctl label{{font-size:.85rem;color:var(--mut)}}
.tcount{{font-size:.8rem;color:var(--mut);margin-left:auto}}
th.srt{{cursor:pointer;user-select:none;white-space:nowrap}} th.srt:hover{{color:var(--fg)}}
.star{{color:#f59e0b;margin-right:3px}}
.ctip{{position:fixed;z-index:99;background:var(--panel);border:1px solid var(--bd);border-radius:8px;padding:5px 10px;font-size:.8rem;color:var(--fg);pointer-events:none;box-shadow:0 4px 12px rgba(0,0,0,.18);white-space:nowrap}}
.ctip b{{font-variant-numeric:tabular-nums}}
svg.chart[data-pts]{{cursor:crosshair}}
footer{{color:var(--mut);font-size:.74rem;text-align:center;margin-top:28px}}
@media(max-width:720px){{.cards{{grid-template-columns:repeat(2,1fr)}}.grid2{{grid-template-columns:1fr}}}}
</style>"""

    script = _table_script.replace("__DATA__", stock_json).replace("__TDATE__", table_date)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = ("<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
           "<meta name='viewport' content='width=device-width,initial-scale=1'>"
           "<title>台股去槓桿壓力儀表板</title></head><body>" + html + script + "</body></html>")
    OUT.write_text(doc, encoding="utf-8")
    build_hist()
    return OUT


# 全市場個股表：搜尋 + 顯示筆數 + 點欄位排序（純 JS，資料由 __DATA__ 注入）
_table_script = """<script>
(function(){
  const D = __DATA__;
  const TD = "__TDATE__";
  window._levD = D;
  let sortK = 2, dir = -1, count = 25, q = "";
  const body = document.getElementById("levBody"), info = document.getElementById("levInfo");
  const fmt = n => (n||0).toLocaleString("en-US");
  function render(){
    let rows = D;
    if(q){ const s=q.toLowerCase(); rows = rows.filter(r => r[0].toLowerCase().includes(s) || String(r[1]).toLowerCase().includes(s)); }
    rows = rows.slice().sort((a,b)=>{
      if(sortK===4||sortK===5){ const ax=a[sortK]!=null, bx=b[sortK]!=null; if(ax!==bx) return ax?-1:1; }  // 缺值墊底
      if(sortK===6){ const ax=a[6]<9999, bx=b[6]<9999; if(ax!==bx) return ax?-1:1; }  // 距追繳無資料墊底
      if(sortK===8){ const ax=a[8]!=null, bx=b[8]!=null; if(ax!==bx) return ax?-1:1; }  // 52週分位缺值墊底
      if(sortK===9){ const ax=a[9]!=null, bx=b[9]!=null; if(ax!==bx) return ax?-1:1; }  // 無斷頭日墊底
      return (a[sortK]<b[sortK]?-1:a[sortK]>b[sortK]?1:0)*dir;
    });
    const shown = count>0 ? rows.slice(0,count) : rows;
    body.innerHTML = shown.map(r=>{
      const star = r[7] ? '<span class="star">★</span>' : '';
      const w = r[3]>=0.1 ? r[3].toFixed(2) : r[3].toFixed(3);
      const mv2 = r[4];  // 融資佔市值（易燃物）
      const mvCls = mv2!=null&&mv2>=8?"hot":(mv2!=null&&mv2>=4?"warm":"");
      const mvTxt = mv2!=null ? mv2.toFixed(2)+"%" : "—";
      const c5 = r[5];   // 近5日漲跌（火苗）
      const c5Cls = c5!=null&&c5<=-10?"hot":(c5!=null&&c5<=-5?"warm":"");
      const c5Txt = c5!=null ? (c5>=0?"+":"")+c5.toFixed(1)+"%" : "—";
      const dc = r[6];   // 距追繳（引信）
      const dcCls = dc>=9999?"":(dc<0?"hot":(dc<5?"warm":""));
      const dcTxt = dc>=9999 ? "—" : (dc<0 ? "已追繳" : "跌"+dc.toFixed(1)+"%");
      const lq = r[9];   // 可能斷頭日
      const lqCls = lq==null?"":(lq<=TD?"hot":"warm");
      const lqTxt = lq==null?"—":(lq<=TD?"處分中":"≈"+lq.slice(5));
      const rk = r[8];   // 融資餘額 52週分位
      const rkCls = rk!=null&&rk>=90?"hot":(rk!=null&&rk>=70?"warm":"");
      const rkTxt = rk!=null ? rk+"%" : "—";
      return '<tr><td class="tk">'+star+'<b>'+r[0]+'</b> '+r[1]+'</td>'
        + '<td class="num">'+w+'%</td>'
        + '<td class="num '+mvCls+'">'+mvTxt+'</td>'
        + '<td class="num '+c5Cls+'">'+c5Txt+'</td>'
        + '<td class="num '+dcCls+'">'+dcTxt+'</td>'
        + '<td class="num '+lqCls+'">'+lqTxt+'</td>'
        + '<td class="num '+rkCls+'">'+rkTxt+'<span class="sub">'+fmt(r[2])+'張</span></td></tr>';
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
// 趨勢圖 hover：游標處顯示日期與數值（十字線＋焦點點＋跟隨 tooltip）
(function(){
  const tip=document.createElement("div"); tip.className="ctip"; tip.style.display="none";
  document.body.appendChild(tip);
  function attachHover(svg){
    let pts; try{ pts=JSON.parse(svg.dataset.pts); }catch(e){ return; }
    if(!pts.length) return;
    const xh=svg.querySelector(".xh"), fc=svg.querySelector(".fc");
    function show(e){
      const r=svg.getBoundingClientRect();
      const vx=(e.clientX-r.left)/r.width*680;
      let best=0,bd=1e9;
      for(let i=0;i<pts.length;i++){ const d=Math.abs(pts[i][0]-vx); if(d<bd){bd=d;best=i;} }
      const p=pts[best];
      xh.setAttribute("x1",p[0]); xh.setAttribute("x2",p[0]); xh.style.display="";
      fc.setAttribute("cx",p[0]); fc.setAttribute("cy",p[1]); fc.style.display="";
      tip.innerHTML=p[2]+"　<b>"+p[3]+"</b>";
      tip.style.display="block";
      const tw=tip.offsetWidth;
      let lx=e.clientX+14; if(lx+tw>window.innerWidth-8) lx=e.clientX-tw-14;
      tip.style.left=lx+"px"; tip.style.top=(e.clientY-34)+"px";
    }
    function hide(){ tip.style.display="none"; xh.style.display="none"; fc.style.display="none"; }
    svg.addEventListener("mousemove",show);
    svg.addEventListener("mouseleave",hide);
    svg.addEventListener("touchstart",e=>{ if(e.touches[0]) show(e.touches[0]); },{passive:true});
    svg.addEventListener("touchmove",e=>{ if(e.touches[0]) show(e.touches[0]); },{passive:true});
  }
  window._attachChartHover=attachHover;
  document.querySelectorAll("svg.chart[data-pts]").forEach(attachHover);
})();
// 個股指標趨勢：選股 + 選指標 → 歷史趨勢圖（資料按需 fetch leverage_hist.json）
(function(){
  const dl=document.getElementById("trList");
  if(!dl) return;
  const D=window._levD||[];
  dl.innerHTML=D.map(r=>'<option value="'+r[0]+' '+r[1]+'">').join("");
  const box=document.getElementById("trChart"), info=document.getElementById("trInfo");
  const stkIn=document.getElementById("trStock"), metSel=document.getElementById("trMetric");
  let H=null, loading=false;
  const CFG={
    bal:{lab:"融資餘額(張)",color:"#3b82f6",fmt:v=>Math.round(v).toLocaleString("en-US")+" 張",yf:v=>Math.round(v).toLocaleString("en-US")},
    dist:{lab:"距追繳",color:"#ef4444",fmt:v=>v<0?"已追繳("+v.toFixed(1)+"%)":"跌"+v.toFixed(1)+"%",yf:v=>v.toFixed(0)+"%",zero:1}
  };
  function series(sid,met){
    const o=H.s[sid]; if(!o) return null;
    const pts=[];
    if(met==="bal"){ H.md.forEach((d,i)=>{ if(o.b[i]!=null) pts.push([d,o.b[i]]); }); }
    else if(met==="dist"){ H.pd.forEach((d,i)=>{ if(o.m[i]!=null) pts.push([d,(o.m[i]-130)/o.m[i]*100]); }); }
    return pts;
  }
  function draw(){
    const q=stkIn.value.trim(); if(!q) return;
    const sid=q.split(/[\s\u3000]/)[0];
    const met=metSel.value, cfg=CFG[met];
    if(!H){ if(!loading){ loading=true; info.textContent="載入歷史數據…";
      fetch("leverage_hist.json").then(r=>r.json()).then(j=>{ H=j; loading=false; info.textContent=""; draw(); })
      .catch(()=>{ info.textContent="歷史數據載入失敗"; loading=false; }); } return; }
    const pts=series(sid,met);
    if(!pts||!pts.length){ box.innerHTML='<p class="note">查無 '+sid+' 的此項資料。</p>'; info.textContent=""; return; }
    const row=D.find(r=>r[0]===sid);
    info.textContent=(row?row[0]+" "+row[1]:sid)+"｜"+cfg.lab+"｜"+pts[0][0]+" ~ "+pts[pts.length-1][0];
    const w=680,h=220,pl=56,prr=14,ptp=14,pb=26,iw=w-pl-prr,ih=h-ptp-pb;
    let lo=Math.min.apply(null,pts.map(p=>p[1])), hi=Math.max.apply(null,pts.map(p=>p[1]));
    if(cfg.zero){ lo=Math.min(lo,0); hi=Math.max(hi,0); }
    let rng=(hi-lo)||1; lo-=rng*0.08; hi+=rng*0.08; rng=hi-lo;
    const X=i=>pl+(pts.length>1?i/(pts.length-1)*iw:0), Y=v=>ptp+ih-(v-lo)/rng*ih;
    const poly=pts.map((p,i)=>X(i).toFixed(1)+","+Y(p[1]).toFixed(1)).join(" ");
    const dp=pts.map((p,i)=>[+X(i).toFixed(1),+Y(p[1]).toFixed(1),p[0],cfg.fmt(p[1])]);
    let sv='<svg viewBox="0 0 '+w+' '+h+'" class="chart" preserveAspectRatio="xMidYMid meet">';
    [0,.5,1].forEach(f=>{ const yv=lo+rng*f,y=Y(yv);
      sv+='<line x1="'+pl+'" y1="'+y.toFixed(1)+'" x2="'+(w-prr)+'" y2="'+y.toFixed(1)+'" class="grid"/>';
      sv+='<text x="'+(pl-6)+'" y="'+(y+3).toFixed(1)+'" class="axis" text-anchor="end">'+cfg.yf(yv)+'</text>'; });
    if(cfg.zero&&lo<0&&hi>0){ const y=Y(0); sv+='<line x1="'+pl+'" y1="'+y.toFixed(1)+'" x2="'+(w-prr)+'" y2="'+y.toFixed(1)+'" stroke="#ef4444" stroke-width="1" stroke-dasharray="4 3" opacity="0.7"/>'; }
    sv+='<polygon points="'+pl+','+(ptp+ih)+' '+poly+' '+X(pts.length-1).toFixed(1)+','+(ptp+ih)+'" fill="'+cfg.color+'" opacity="0.12"/>';
    sv+='<polyline points="'+poly+'" fill="none" stroke="'+cfg.color+'" stroke-width="2" stroke-linejoin="round"/>';
    sv+='<circle cx="'+X(pts.length-1).toFixed(1)+'" cy="'+Y(pts[pts.length-1][1]).toFixed(1)+'" r="3.2" fill="'+cfg.color+'"/>';
    sv+='<text x="'+pl+'" y="'+(h-6)+'" class="axis" text-anchor="start">'+pts[0][0].slice(5)+'</text>';
    sv+='<text x="'+(w-prr)+'" y="'+(h-6)+'" class="axis" text-anchor="end">'+pts[pts.length-1][0].slice(5)+'</text>';
    sv+='<line class="xh" x1="0" x2="0" y1="'+ptp+'" y2="'+(ptp+ih)+'" stroke="var(--mut)" stroke-width="1" stroke-dasharray="3 3" style="display:none"/>';
    sv+='<circle class="fc" r="4" fill="'+cfg.color+'" stroke="var(--bg2)" stroke-width="1.5" style="display:none"/>';
    sv+='</svg>';
    box.innerHTML=sv;
    const el=box.querySelector("svg");
    el.dataset.pts=JSON.stringify(dp);
    if(window._attachChartHover) window._attachChartHover(el);
  }
  stkIn.addEventListener("change",draw);
  metSel.addEventListener("change",draw);
  stkIn.value="2330 台積電"; draw();
})();
</script>"""


if __name__ == "__main__":
    p = build()
    print(f"✅ 產生 {p}")
