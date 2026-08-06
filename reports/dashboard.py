"""公司儀表板產生器：每檔一頁靜態 HTML ＋ 總覽 index。

    python -m reports.main dashboard [股號 ...]   # 未給股號跑 config 全清單

資料組成（皆可回溯）：FinMind 三表 8 季（損益單季、資產負債時點、現金流年內差分）、
nowcast 四季展望＋損益模型、營收組成（財報附註 Haiku 萃取，快取 _segments.json）、
結構訊號/CL 交叉檢核、重訊時間軸（data/announcements ＋判讀快取）。
輸出 reports_data/dashboard/，本機服務：python -m http.server 8811 --bind 127.0.0.1 -d reports_data/dashboard
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

from .config import ReportsConfig
from . import finmind_client, llm
from .announcements import STORE_DIR
from .events import INTERPRETED
from .nowcast import nowcast_stock, _pl_model, _cl_signal, _structural_signals

logger = logging.getLogger(__name__)

SEGMENT_SYSTEM = (
    "你是資料萃取器。以下是台股公司最新一季財報中與收入細分/部門資訊相關的節錄。"
    "請找出「最新一季（節錄開頭標示的季度）」揭露最完整的一個營收拆分維度"
    "（優先順序：產品/部門別 > 地區別），只輸出一個 JSON 物件：\n"
    '{"dimension": "維度名稱(如 部門別/地區別)", "period": "如 2026Q1", '
    '"items": [{"label": "類別名", "value": 金額仟元}, ...]}\n'
    "規則：金額用仟元；只放同一維度、同一期間的項目；合計/小計不要放；"
    "找不到可用拆分就輸出 {\"items\": []}。不要杜撰。"
)

_KW = ["收入之細分", "收入細分", "部門資訊", "營運部門", "客戶合約", "地區"]


def _segments(cfg: ReportsConfig, stock: str, api_key: str) -> dict | None:
    cache = cfg.data_dir / "analysis" / f"{stock}_segments.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    txts = sorted((cfg.data_dir / "text" / stock).glob("*.txt")) if (cfg.data_dir / "text" / stock).exists() else []
    if not txts:
        return None
    latest = txts[-1]
    lines = latest.read_text(encoding="utf-8").splitlines()
    hits = [i for i, ln in enumerate(lines) if any(k in ln.replace(" ", "") for k in _KW)]
    if not hits:
        return None
    ranges: list[list[int]] = []
    for i in hits:
        lo, hi = max(0, i - 25), min(len(lines), i + 26)
        if ranges and lo <= ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], hi)
        else:
            ranges.append([lo, hi])
    excerpt = "\n…\n".join("\n".join(lines[lo:hi]) for lo, hi in ranges)[:30000]
    quarter = latest.stem.split("_")[0]
    res = llm.chat(cfg.cheap_model, SEGMENT_SYSTEM, f"季度：{quarter}\n\n{excerpt}", api_key)
    m = re.search(r"\{.*\}", res["text"], re.S)
    try:
        data = json.loads(m.group(0)) if m else {"items": []}
    except json.JSONDecodeError:
        data = {"items": []}
    cache.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def _announcements_for(stock: str, days: int = 120) -> list[dict]:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    interp = {}
    if INTERPRETED.exists():
        for line in INTERPRETED.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                interp[r["key"]] = r
            except (json.JSONDecodeError, KeyError):
                continue
    out = []
    for p in sorted(STORE_DIR.glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("stock_id") == stock and r.get("date", "") >= cutoff:
                r["interp"] = interp.get(r["key"])
                out.append(r)
    return sorted(out, key=lambda x: x["date"], reverse=True)


def _stock_name(stock: str, token: str) -> str:
    try:
        rows = finmind_client._fetch("TaiwanStockInfo", stock, "", token)
        return rows[0]["stock_name"] if rows else ""
    except Exception:  # noqa: BLE001
        return ""


def _fmt(v, div=1e6, nd=0):
    if v is None:
        return "—"
    return f"{v / div:,.{nd}f}"


def _rng(t, nd=2):
    return f"[{t[0]:,.{nd}f}, {t[1]:,.{nd}f}]" if t else "—"


_CSS = """
body{font-family:-apple-system,'PingFang TC',sans-serif;margin:0;background:#f6f5f1;color:#222;line-height:1.55}
.wrap{max-width:1080px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:26px;margin:8px 0 2px} h2{font-size:17px;margin:34px 0 10px;border-left:4px solid #534AB7;padding-left:10px}
.sub{color:#777;font-size:13px} a{color:#534AB7;text-decoration:none}
table{border-collapse:collapse;width:100%;font-size:13.5px;background:#fff;border-radius:8px;overflow:hidden}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid #eee;white-space:nowrap}
th{background:#efede6;font-weight:500} td:first-child,th:first-child{text-align:left}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}
.card{background:#fff;border-radius:10px;padding:16px 18px;border:1px solid #e6e3da}
.note{font-size:12.5px;color:#888;margin-top:8px}
.tag{display:inline-block;font-size:11.5px;padding:1px 8px;border-radius:9px;margin-left:6px}
.hi{background:#e1f5ee;color:#085041}.mid{background:#faeeda;color:#633806}.lo{background:#f1efe8;color:#444}
.mat{background:#fcebeb;color:#791f1f}.rout{background:#f1efe8;color:#666}
.scroll{overflow-x:auto}
@media(max-width:820px){.grid{grid-template-columns:1fr}}
"""


def _income_section(inc: list[dict]) -> tuple[str, dict]:
    q = inc[-8:]
    labels = [r["date"][:7].replace("-0", "Q").replace("-1", "Q1") for r in q]
    labels = [f"{r['date'][:4]}Q{(int(r['date'][5:7]) - 1) // 3 + 1}" for r in q]
    rev = [round(r.get("Revenue", 0) / 1e6) if r.get("Revenue") else None for r in q]
    gm = [round(r["GrossProfit"] / r["Revenue"] * 100, 1) if r.get("GrossProfit") and r.get("Revenue") else None for r in q]
    rows = ""
    items = [("營業收入", "Revenue"), ("營業毛利", "GrossProfit"), ("營業費用", "OperatingExpenses"),
             ("營業利益", "OperatingIncome"), ("業外損益", "TotalNonoperatingIncomeAndExpense"),
             ("稅前淨利", "PreTaxIncome"), ("稅後淨利", "IncomeAfterTaxes")]
    for name, key in items:
        rows += f"<tr><td>{name}</td>" + "".join(f"<td>{_fmt(r.get(key))}</td>" for r in q) + "</tr>"
    rows += "<tr><td>毛利率</td>" + "".join(f"<td>{g if g is not None else '—'}%</td>" for g in gm) + "</tr>"
    rows += "<tr><td>EPS(元)</td>" + "".join(f"<td>{r.get('EPS', '—')}</td>" for r in q) + "</tr>"
    html = (f"<div class='scroll'><table><tr><th>百萬元</th>" +
            "".join(f"<th>{l}</th>" for l in labels) + f"</tr>{rows}</table></div>")
    return html, {"labels": labels, "rev": rev, "gm": gm}


def _kv_table(series: list[dict], mapping: dict[str, str]) -> str:
    q = series[-8:]
    labels = [f"{r['date'][:4]}Q{(int(r['date'][5:7]) - 1) // 3 + 1}" for r in q]
    rows = ""
    for key, name in mapping.items():
        if not any(r.get(key) is not None for r in q):
            continue  # 該公司未單列的科目整列隱藏
        rows += f"<tr><td>{name}</td>" + "".join(f"<td>{_fmt(r.get(key))}</td>" for r in q) + "</tr>"
    return (f"<div class='scroll'><table><tr><th>百萬元</th>" +
            "".join(f"<th>{l}</th>" for l in labels) + f"</tr>{rows}</table></div>")


def build_stock_page(cfg: ReportsConfig, stock: str, token: str, api_key: str) -> dict:
    name = _stock_name(stock, token)
    inc = finmind_client.quarterly_income(stock, token)
    bs = finmind_client.balance_sheet_series(stock, token)
    cf = finmind_client.cash_flow_series(stock, token)
    nc = nowcast_stock(stock, token)
    pl = _pl_model(stock, token)
    cl_sig = _cl_signal(cfg.data_dir, stock)
    struct = _structural_signals(cfg.data_dir, stock)
    seg = _segments(cfg, stock, api_key)
    anns = _announcements_for(stock)

    def eps(rng):
        return pl["eps_range"](rng) if pl and rng and not pl.get("unstable") else None

    fc_rows = ""
    if nc:
        for label, rng, conf, cls in ((f"{nc['quarter']} (T)", nc["cur"], "高", "hi"),
                                      (f"{nc['next_quarter']} (T+1)", nc["next"], "中", "mid"),
                                      (f"{nc['t2_quarter']} (T+2)", nc["t2"], "低", "lo")):
            e = eps(rng)
            fc_rows += (f"<tr><td>{label}<span class='tag {cls}'>{conf}</span></td>"
                        f"<td>{_rng((rng[0] / 1000, rng[1] / 1000), 0) if rng else '—'}</td>"
                        f"<td>{_rng(e) if e else ('拒估' if pl and pl.get('unstable') else '—')}</td></tr>")
    model_note = ""
    if pl and pl.get("unstable"):
        model_note = f"損益模型拒估：近 4 季毛利率 {_rng(pl['gm'], 3)} 含深度負值（減損污染），線性外推必失真。"
    elif pl:
        model_note = (f"模型：毛利率 {_rng((pl['gm'][0] * 100, pl['gm'][1] * 100), 1)}%、"
                      f"{pl['opex']['mode']}、業外中位 {_fmt(pl['nonop'] * 1000)} 百萬、稅率 {pl['tax']:.0%}、"
                      f"母公司比率 {pl['parent']:.2f}、股數 {pl['shares']:,}"
                      + (f"、回測 MAE {pl['mae']:.2f} 元" if pl.get("mae") is not None else ""))

    inc_html, chart = _income_section(inc) if inc else ("<p class='note'>無季損益資料</p>", {})
    bs_html = _kv_table(bs, {"CashAndCashEquivalents": "現金及約當現金", "AccountsReceivableNet": "應收帳款",
                             "Inventories": "存貨", "CurrentContractLiabilities": "合約負債(流動)",
                             "TotalAssets": "資產總額", "Equity": "權益總額"}) if bs else "<p class='note'>無資料</p>"
    cf_html = _kv_table(cf, {"NetCashInflowFromOperatingActivities": "營運現金流",
                             "CashProvidedByInvestingActivities": "投資現金流",
                             "CashFlowsProvidedFromFinancingActivities": "籌資現金流",
                             "PropertyAndPlantAndEquipment": "取得不動產廠房設備",
                             "Depreciation": "折舊"}) if cf else "<p class='note'>無資料</p>"

    sig_html = ""
    if struct:
        sig_html += "<ul>" + "".join(f"<li>{n} QoQ {p:+.0%}：{note}</li>" for n, p, note in struct["signals"])
        sig_html += f"<li><b>綜合傾向：T+1 落點{struct['lean']}</b>（票決 {struct['votes']:+d}）</li></ul>"
    if cl_sig and cl_sig.get("rates"):
        r_lo, r_hi = cl_sig["rates"]
        sig_html += (f"<p>合約負債交叉檢核：{cl_sig['quarter']} 期末 {_fmt(cl_sig['cl'] * 1000)} 百萬 × "
                     f"轉換率 [{r_lo:.0%}, {r_hi:.0%}] → 次季隱含 "
                     f"{_fmt(cl_sig['cl'] * r_lo * 1000)}–{_fmt(cl_sig['cl'] * r_hi * 1000)} 百萬</p>")
    if not sig_html:
        sig_html = "<p class='note'>尚無 cl 事實卡（未跑過 cl 深讀），無結構訊號。</p>"

    ann_html = ""
    for a in anns[:15]:
        tag = ""
        if a.get("interp"):
            i = a["interp"]
            tag = (f"<span class='tag mat'>{i['impact_type']}/{i['direction']}/{i['magnitude']}</span>"
                   if i.get("material") else "<span class='tag rout'>例行</span>")
        ann_html += f"<li>{a['date']}　{a['subject'][:60]}{tag}</li>"
    ann_html = f"<ul style='font-size:13.5px'>{ann_html}</ul>" if ann_html else "<p class='note'>近 120 天無重訊（本地庫）。</p>"

    seg_json = json.dumps(seg, ensure_ascii=False) if seg and seg.get("items") else "null"
    chart_json = json.dumps(chart, ensure_ascii=False)
    cf_chart = json.dumps({"labels": [f"{r['date'][:4]}Q{(int(r['date'][5:7]) - 1) // 3 + 1}" for r in cf[-8:]],
                           "op": [round(r["NetCashInflowFromOperatingActivities"] / 1e6)
                                  if r.get("NetCashInflowFromOperatingActivities") else None for r in cf[-8:]],
                           "inv": [round(r["CashProvidedByInvestingActivities"] / 1e6)
                                   if r.get("CashProvidedByInvestingActivities") else None for r in cf[-8:]],
                           "fin": [round(r["CashFlowsProvidedFromFinancingActivities"] / 1e6)
                                   if r.get("CashFlowsProvidedFromFinancingActivities") else None for r in cf[-8:]]},
                          ensure_ascii=False) if cf else "null"

    html = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{stock} {name} 公司檔案</title><style>{_CSS}</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script></head>
<body><div class="wrap">
<p class="sub"><a href="index.html">← 總覽</a></p>
<h1>{stock} {name}</h1>
<p class="sub">產生於 {date.today().isoformat()}・資料源：FinMind／MOPS 財報附註／重訊庫・所有推估為模型值非投資建議</p>

<h2>四季展望（EPS 預估）</h2>
<table><tr><th>季度</th><th>營收區間(百萬)</th><th>EPS 區間(元)</th></tr>{fc_rows or "<tr><td colspan=3>無月營收資料</td></tr>"}</table>
<p class="note">{model_note}</p>

<h2>訊號</h2><div class="card">{sig_html}</div>

<div class="grid">
<div><h2>營收與毛利率（8 季）</h2><div class="card"><canvas id="revChart" height="210"></canvas></div></div>
<div><h2>營收組成{f"（{seg.get('dimension','')}，{seg.get('period','')}）" if seg and seg.get('items') else ""}</h2>
<div class="card"><canvas id="pieChart" height="210"></canvas>
{"" if seg and seg.get('items') else "<p class='note'>財報附註無可用拆分或尚未萃取。</p>"}</div></div>
</div>

<h2>損益表（單季）</h2>{inc_html}
<h2>資產負債關鍵科目（期末）</h2>{bs_html}
<h2>現金流量（單季，年內差分還原）</h2>{cf_html}
<div class="card" style="margin-top:14px"><canvas id="cfChart" height="120"></canvas></div>

<h2>重訊時間軸（近 120 天）</h2>{ann_html}

<script>
const C = {chart_json}, SEG = {seg_json}, CF = {cf_chart};
if (C.labels) new Chart(revChart, {{type:'bar', data:{{labels:C.labels, datasets:[
 {{label:'營收(百萬)', data:C.rev, backgroundColor:'#AFA9EC', yAxisID:'y'}},
 {{label:'毛利率%', data:C.gm, type:'line', borderColor:'#D85A30', yAxisID:'y2', tension:.2}}]}},
 options:{{scales:{{y:{{position:'left'}}, y2:{{position:'right', grid:{{display:false}}}}}}}}}});
if (SEG) new Chart(pieChart, {{type:'doughnut', data:{{labels:SEG.items.map(i=>i.label),
 datasets:[{{data:SEG.items.map(i=>i.value), backgroundColor:['#534AB7','#1D9E75','#D85A30','#D4537E','#378ADD','#BA7517','#888780','#97C459']}}]}},
 options:{{plugins:{{legend:{{position:'right'}}}}}}}});
if (CF) new Chart(cfChart, {{type:'bar', data:{{labels:CF.labels, datasets:[
 {{label:'營運', data:CF.op, backgroundColor:'#1D9E75'}},
 {{label:'投資', data:CF.inv, backgroundColor:'#D85A30'}},
 {{label:'籌資', data:CF.fin, backgroundColor:'#888780'}}]}}}});
</script>
</div></body></html>"""

    out = cfg.data_dir / "dashboard" / f"{stock}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    e_cur = eps(nc["cur"]) if nc else None
    e_n1 = eps(nc["next"]) if nc else None
    return {"stock": stock, "name": name, "quarter": nc["quarter"] if nc else "—",
            "rev": nc["known_sum"] if nc else None, "eps": e_cur, "eps_n1": e_n1,
            "mae": pl.get("mae") if pl else None, "unstable": bool(pl and pl.get("unstable"))}


def run_dashboard(cfg: ReportsConfig, stocks: list[str] | None = None) -> int:
    token = finmind_client.get_token()
    api_key = llm.get_api_key()
    rows = []
    for s in stocks or cfg.stocks:
        try:
            rows.append(build_stock_page(cfg, s, token, api_key))
            logger.info("  %s ✓", s)
        except Exception as exc:  # noqa: BLE001
            logger.warning("  %s 頁面產生失敗：%s", s, exc)
    trs = ""
    for r in rows:
        eps_s = "拒估" if r["unstable"] else (_rng(r["eps"]) if r["eps"] else "—")
        mae_s = f"{r['mae']:.2f}" if r["mae"] is not None else "—"
        trs += (f"<tr><td><a href='{r['stock']}.html'>{r['stock']} {r['name']}</a></td>"
                f"<td>{r['quarter']}</td><td>{_fmt(r['rev'], 1e3)}</td><td>{eps_s}</td>"
                f"<td>{_rng(r['eps_n1']) if r['eps_n1'] else '—'}</td>"
                f"<td>{mae_s}</td></tr>")
    html = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>公司檔案總覽</title><style>{_CSS}</style></head><body><div class="wrap">
<h1>公司檔案總覽</h1><p class="sub">產生於 {date.today().isoformat()}・模型值非投資建議</p>
<table><tr><th>公司</th><th>T 季度</th><th>T 營收(百萬)</th><th>T EPS 區間</th><th>T+1 EPS 區間</th><th>回測 MAE</th></tr>{trs}</table>
</div></body></html>"""
    (cfg.data_dir / "dashboard" / "index.html").write_text(html, encoding="utf-8")
    logger.info("dashboard 完成 %d 檔 → %s", len(rows), cfg.data_dir / "dashboard")
    return 0
