"""公司儀表板產生器：每檔一頁靜態 HTML ＋ 總覽 index。

    python -m reports.main dashboard [股號 ...]   # 未給股號跑 config 全清單

資料組成（皆可回溯）：FinMind 三表 8 季（損益單季、資產負債時點、現金流年內差分）、
nowcast 四季展望＋損益模型、營收組成（財報附註 Haiku 萃取，快取 _segments.json）、
結構訊號/CL 交叉檢核、重訊時間軸（data/announcements ＋判讀快取）。
輸出 reports_data/dashboard/，本機服務：python -m http.server 8811 --bind 127.0.0.1 -d reports_data/dashboard
"""
from __future__ import annotations

import hashlib
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
    "請找出「最新一季（節錄開頭標示的季度）」的營收拆分，維度必須是"
    "**產品別/服務別/業務部門別**（公司靠什麼工作/服務賺錢），"
    "【絕對不要用地區別/國家別】。只輸出一個 JSON 物件：\n"
    '{"dimension": "維度名稱(如 部門別/產品別/服務別)", "period": "如 2026Q1", '
    '"items": [{"label": "類別名", "value": 金額仟元}, ...], "note": "備註或空字串"}\n'
    "規則：金額用仟元；只放同一維度、同一期間的項目；合計/小計不要放；"
    "若有多個可用維度（如產品別、技術平台別、部門別），**選類別數最多、資訊量最大的那個**"
    "（例如「晶圓/其他」兩類 vs 平台別六類 → 選平台別；百分比拆分也可以，value 填百分比數值並在 note 註明「百分比」）；"
    "若財報只揭露地區別、沒有產品/服務拆分，輸出 {\"items\": [], \"note\": \"僅揭露地區別\"}。不要杜撰。"
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


COMMENT_SYSTEM = (
    "你是資深投資分析師。以下是一檔台股的程式化預估數據包（四季展望、損益模型參數、"
    "資產負債/現金流量預估法與近期數值、結構訊號、合約負債檢核、營收組成、近期重訊、過往深讀結論）。"
    "請寫三段「報表預估解讀」，分別對應三張報表，各段以獨立一行的分隔符開頭（一字不差）：\n"
    "===損益表===\n"
    "## 這張表怎麼估\n- 說明預估欄數字的來源：營收基礎（月營收實績/季節因子/合約負債轉換）、"
    "毛利率假設、費用擬合、業外與稅——為什麼中值是這個數。\n"
    "## 支撐與壓力\n- 上緣靠什麼訊號、下緣的風險（引用結構訊號/檢核/重訊）。\n"
    "## 驗證點\n- 哪個時點看什麼數據可確認或推翻（月營收 10 日、財報日、特定重訊）。\n"
    "最後一行以「#! 」開頭寫結論大標：一個有明確立場的判斷句（≤28 字），"
    "直接下判斷，禁止「一句話」「總結」「結論」等前綴詞。\n"
    "===資產負債===\n"
    "- 說明預估法（應收/存貨＝佔營收比中位×預估營收）與哪些科目不估、為何。\n"
    "- 判讀近期科目變化的訊號意義（如應收暴增、存貨結構、合約負債方向），連結到營收預估的可信度。\n"
    "===現金流量===\n"
    "- 說明預估法（近 8 季中位粗估）與侷限。\n"
    "- 判讀營運現金流 vs 帳面獲利的落差、資本支出節奏透露的訊息。\n"
    "鐵律：只根據數據包內容，數字要與數據包一致並標來源（如「模型」「月營收」「重訊 07-31」）；"
    "不引入外部知識、不杜撰；低可信(volatile)的檔要明講模型侷限。繁體中文、精簡有判斷。"
)


def _md2html(md: str) -> str:
    """極簡 markdown → HTML（小標/條列/粗體），配合 X bot 評論格式。"""
    out, in_ul = [], False
    for ln in md.splitlines():
        ln = ln.strip()
        ln = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", ln)
        if ln.startswith("#! "):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(f"<h3 class='concl'>{ln[3:]}</h3>")
        elif ln.startswith("## "):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(f"<h3>{ln[3:]}</h3>")
        elif ln.startswith("- "):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{ln[2:]}</li>")
        elif ln:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(f"<p>{ln}</p>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def _cl_oneliner(cfg: ReportsConfig, stock: str) -> str:
    """cl 深讀報告的「一句話」結論（若有）。"""
    files = sorted((cfg.data_dir / "analysis").glob(f"{stock}_contract_liability_*.md"))
    if not files:
        return ""
    m = re.findall(r"一句話[：:]\**\s*(.+)", files[-1].read_text(encoding="utf-8"))
    return m[-1].strip()[:300] if m else ""


def _comment(cfg: ReportsConfig, stock: str, payload: dict, api_key: str) -> str:
    """Opus 預估解讀，數據指紋快取：payload 沒變就不重新生成。"""
    cache = cfg.data_dir / "analysis" / f"{stock}_comment.json"
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    # 指紋含 prompt：改提示詞時全部重生成，不然舊格式評論會殘留
    fp = hashlib.sha1((COMMENT_SYSTEM + blob).encode()).hexdigest()[:16]
    if cache.exists():
        try:
            old = json.loads(cache.read_text(encoding="utf-8"))
            if old.get("fp") == fp:
                return old["text"]
        except json.JSONDecodeError:
            pass
    res = llm.chat(cfg.comment_model, COMMENT_SYSTEM, f"股票：{stock}\n\n數據包：\n{blob}", api_key)
    cache.write_text(json.dumps({"fp": fp, "date": date.today().isoformat(), "text": res["text"]},
                                ensure_ascii=False), encoding="utf-8")
    logger.info("  %s AI 判讀（%s）✓ $%.4f", stock, cfg.comment_model, res.get("cost") or 0)
    return res["text"]


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
    return f"{t[0]:,.{nd}f}~{t[1]:,.{nd}f}" if t else "—"


def _mid_rng(rng, div=1.0, nd=0):
    """區間呈現：粗體中值＋淡色範圍（lo~hi）；單點只印值。"""
    if not rng:
        return "—"
    lo, hi = rng[0] / div, rng[1] / div
    if abs(hi - lo) < 10 ** -nd / 2:
        return f"{lo:,.{nd}f}"
    return f"<b>{(lo + hi) / 2:,.{nd}f}</b> <span class='rng'>{lo:,.{nd}f}~{hi:,.{nd}f}</span>"


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
th.est{background:#EEEDFE;color:#3C3489} td.est{background:#f7f6fd;color:#3C3489}
.rng{color:#999;font-size:12px;white-space:nowrap}
h3.concl{border-top:1px solid #eee;padding-top:10px;margin:14px 0 2px;font-size:15.5px}
@media(max-width:820px){.grid{grid-template-columns:1fr}}
"""


def _q_label(date_str: str) -> str:
    return f"{date_str[:4]}Q{(int(date_str[5:7]) - 1) // 3 + 1}"


def _mid(rng):
    return (rng[0] + rng[1]) / 2 if rng else None


def _fc_rev_cols(nc, hist_labels: list[str]) -> list[tuple[str, float]]:
    """預估季欄位（最遠的放最前=最左）：(季度標籤, 營收中值仟元)。排除已有歷史的季。"""
    if not nc:
        return []
    cand = [(nc["t3_quarter"], nc["t3"]), (nc["t2_quarter"], nc["t2"]),
            (nc["next_quarter"], nc["next"]), (nc["quarter"], nc["cur"])]
    return [(lbl, _mid(rng)) for lbl, rng in cand if rng and lbl not in hist_labels]


def _render_table(cols: list[dict], rows_def: list[tuple[str, str]], extra_rows: str = "") -> str:
    """cols＝由左至右的欄（含 est 標記），rows_def＝[(顯示名, key)]；值為 None 顯示 —。"""
    head = "<tr><th>百萬元</th>" + "".join(
        f"<th class='{'est' if c.get('est') else ''}'>{c['label']}{'<br>預估' if c.get('est') else ''}</th>"
        for c in cols) + "</tr>"
    body = ""
    for name, key in rows_def:
        if not any(c.get(key) is not None for c in cols):
            continue  # 該公司未單列的科目整列隱藏
        body += f"<tr><td>{name}</td>" + "".join(
            f"<td class='{'est' if c.get('est') else ''}'>{_fmt(c.get(key))}</td>" for c in cols) + "</tr>"
    return f"<div class='scroll'><table>{head}{body}{extra_rows}</table></div>"


_INCOME_ROWS = [("營業收入", "Revenue"), ("營業毛利", "GrossProfit"), ("營業費用", "OperatingExpenses"),
                ("營業利益", "OperatingIncome"), ("業外損益", "TotalNonoperatingIncomeAndExpense"),
                ("稅前淨利", "PreTaxIncome"), ("稅後淨利", "IncomeAfterTaxes")]


def _income_table(inc: list[dict], pl, nc) -> str:
    hist = [dict(r, label=_q_label(r["date"])) for r in inc[-8:]][::-1]
    fc_cols = []
    for lbl, rev_k in _fc_rev_cols(nc, [h["label"] for h in hist]):
        col = {"label": lbl, "est": True, "Revenue": rev_k * 1000}
        if pl:
            gm_mid = pl["gm_mid"]
            gp = rev_k * gm_mid
            opex = pl["opex"]["a"] + pl["opex"]["b"] * rev_k
            op = gp - opex
            pre = op + pl["nonop"]
            net = pre * (1 - pl["tax"])
            col.update({"GrossProfit": gp * 1000, "OperatingExpenses": opex * 1000,
                        "OperatingIncome": op * 1000, "TotalNonoperatingIncomeAndExpense": pl["nonop"] * 1000,
                        "PreTaxIncome": pre * 1000, "IncomeAfterTaxes": net * 1000,
                        "_gm": round(gm_mid * 100, 1),
                        "EPS": round(net * pl["parent"] * 1000 / pl["shares"], 2)})
        fc_cols.append(col)
    for h in hist:
        if h.get("GrossProfit") and h.get("Revenue"):
            h["_gm"] = round(h["GrossProfit"] / h["Revenue"] * 100, 1)
    cols = fc_cols + hist
    gm_row = "<tr><td>毛利率</td>" + "".join(
        f"<td class='{'est' if c.get('est') else ''}'>{str(c['_gm']) + '%' if c.get('_gm') is not None else '—'}</td>"
        for c in cols) + "</tr>"
    eps_row = "<tr><td>EPS(元)</td>" + "".join(
        f"<td class='{'est' if c.get('est') else ''}'>{c.get('EPS', '—') if c.get('EPS') is not None else '—'}</td>"
        for c in cols) + "</tr>"
    return _render_table(cols, _INCOME_ROWS, gm_row + eps_row)


def _income_chart(inc: list[dict]) -> dict:
    q = inc[-8:]
    return {"labels": [_q_label(r["date"]) for r in q],
            "rev": [round(r["Revenue"] / 1e6) if r.get("Revenue") else None for r in q],
            "gm": [round(r["GrossProfit"] / r["Revenue"] * 100, 1)
                   if r.get("GrossProfit") and r.get("Revenue") else None for r in q]}


_BS_ROWS = [("現金及約當現金", "CashAndCashEquivalents"), ("應收帳款", "AccountsReceivableNet"),
            ("存貨", "Inventories"), ("合約負債(流動)", "CurrentContractLiabilities"),
            ("資產總額", "TotalAssets"), ("權益總額", "Equity")]


def _bs_table(bs: list[dict], inc: list[dict], nc) -> tuple[str, dict]:
    hist = [dict(r, label=_q_label(r["date"])) for r in bs[-8:]][::-1]
    rev_by_date = {r["date"]: r.get("Revenue") for r in inc}
    ratios = {}
    for key in ("AccountsReceivableNet", "Inventories"):
        rs = sorted(r[key] / rev_by_date[r["date"]] for r in bs[-4:]
                    if r.get(key) and rev_by_date.get(r["date"]))
        ratios[key] = rs[len(rs) // 2] if rs else None
    fc_cols = []
    for lbl, rev_k in _fc_rev_cols(nc, [h["label"] for h in hist]):
        col = {"label": lbl, "est": True}
        for key, ratio in ratios.items():
            if ratio:
                col[key] = ratio * rev_k * 1000
        fc_cols.append(col)
    return _render_table(fc_cols + hist, _BS_ROWS), ratios


_CF_ROWS = [("營運現金流", "NetCashInflowFromOperatingActivities"),
            ("投資現金流", "CashProvidedByInvestingActivities"),
            ("籌資現金流", "CashFlowsProvidedFromFinancingActivities"),
            ("取得不動產廠房設備", "PropertyAndPlantAndEquipment"), ("折舊", "Depreciation")]


def _cf_table(cf: list[dict], nc) -> str:
    hist = [dict(r, label=_q_label(r["date"])) for r in cf[-8:]][::-1]
    med = {}
    for _, key in _CF_ROWS:
        vals = sorted(r[key] for r in cf[-8:] if r.get(key) is not None)
        med[key] = vals[len(vals) // 2] if vals else None
    fc_cols = [dict({"label": lbl, "est": True}, **med)
               for lbl, _ in _fc_rev_cols(nc, [h["label"] for h in hist])]
    return _render_table(fc_cols + hist, _CF_ROWS), med


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
        return pl["eps_range"](rng) if pl and rng else None

    fc_rows = ""
    if nc:
        for label, rng, conf, cls in ((f"{nc['quarter']} (T)", nc["cur"], "高", "hi"),
                                      (f"{nc['next_quarter']} (T+1)", nc["next"], "中", "mid"),
                                      (f"{nc['t2_quarter']} (T+2)", nc["t2"], "低", "lo"),
                                      (f"{nc['t3_quarter']} (T+3)", nc["t3"], "極低", "lo")):
            e = eps(rng)
            fc_rows += (f"<tr><td>{label}<span class='tag {cls}'>{conf}</span></td>"
                        f"<td>{_mid_rng(rng, 1000, 0)}</td>"
                        f"<td>{_mid_rng(e, 1, 2)}</td></tr>")
    model_note = ""
    if pl:
        model_note = (f"模型：毛利率 {_rng((pl['gm'][0] * 100, pl['gm'][1] * 100), 1)}%（近8季P25–P75截尾）、"
                      f"{pl['opex']['mode']}、業外中位 {_fmt(pl['nonop'] * 1000)} 百萬、稅率 {pl['tax']:.0%}、"
                      f"母公司比率 {pl['parent']:.2f}、股數 {pl['shares']:,}"
                      + (f"、回測 MAE {pl['mae']:.2f} 元" if pl.get("mae") is not None else "")
                      + ("。⚠ 近 8 季含深度負毛利季，毛利率採穩健區間，EPS 低可信"
                         if pl.get("volatile") else ""))

    inc_html = _income_table(inc, pl, nc) if inc else "<p class='note'>無季損益資料</p>"
    chart = _income_chart(inc) if inc else {}
    bs_html, bs_ratios = _bs_table(bs, inc, nc) if bs else ("<p class='note'>無資料</p>", {})
    cf_html, cf_med = _cf_table(cf, nc) if cf else ("<p class='note'>無資料</p>", {})

    sig_html = ""
    if struct:
        sig_html += "<ul>" + "".join(f"<li>{n} QoQ {p:+.0%}：{note}</li>" for n, p, note in struct["signals"])
        sig_html += f"<li><b>綜合傾向：T+1 落點{struct['lean']}</b>（票決 {struct['votes']:+d}）</li></ul>"
    if cl_sig and cl_sig.get("rates"):
        r_lo, r_hi = cl_sig["rates"]
        sig_html += (f"<p>合約負債交叉檢核：{cl_sig['quarter']} 期末 {_fmt(cl_sig['cl'] * 1000)} 百萬 × "
                     f"轉換率 {r_lo:.0%}~{r_hi:.0%} → 次季隱含 "
                     f"{_fmt(cl_sig['cl'] * r_lo * 1000)}~{_fmt(cl_sig['cl'] * r_hi * 1000)} 百萬</p>")
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

    payload = {
        "四季展望": [{"季度": lbl, "營收區間仟元": rng, "EPS區間": eps(rng), "信心": conf}
                  for lbl, rng, conf in (((nc["quarter"], nc["cur"], "高(月營收)"),
                                          (nc["next_quarter"], nc["next"], "中(季節因子)"),
                                          (nc["t2_quarter"], nc["t2"], "低(因子鏈)"),
                                          (nc["t3_quarter"], nc["t3"], "極低(因子鏈三段)")) if nc else [])],
        "損益模型": ({"毛利率區間": pl["gm"], "毛利率中位": pl["gm_mid"], "費用": pl["opex"]["mode"],
                   "業外中位仟元": pl["nonop"], "稅率": pl["tax"], "母公司比率": pl["parent"],
                   "回測MAE元": pl["mae"], "volatile低可信": pl["volatile"]} if pl else None),
        "結構訊號": struct, "合約負債檢核": cl_sig, "營收組成": seg,
        "資產負債預估法": {"方法": "應收/存貨＝佔營收比近4季中位×預估營收，其餘科目不預估",
                     "比率": bs_ratios,
                     "近2季科目(元)": [{k: r.get(k) for k in
                                   ("date", "CashAndCashEquivalents", "AccountsReceivableNet",
                                    "Inventories", "CurrentContractLiabilities")} for r in bs[-2:]]},
        "現金流量預估法": {"方法": "各科目近8季中位數粗估", "中位數(元)": cf_med,
                     "近4季(元)": cf[-4:]},
        "近期重訊": [{"日期": a["date"], "主旨": a["subject"][:60],
                  "判讀": (a["interp"] or {}).get("summary")} for a in anns[:8]],
        "cl深讀結論": _cl_oneliner(cfg, stock),
    }
    c_inc = c_bs = c_cf = "<p class='note'>本次未產生（LLM 呼叫失敗），重跑 dashboard 可補。</p>"
    try:
        raw = _comment(cfg, stock, payload, api_key)
        parts = re.split(r"===\s*(損益表|資產負債|現金流量)\s*===", raw)
        blocks = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
        if blocks:
            c_inc = _md2html(blocks.get("損益表", ""))
            c_bs = _md2html(blocks.get("資產負債", ""))
            c_cf = _md2html(blocks.get("現金流量", ""))
        else:  # 模型沒照分隔符輸出時整段放損益表下
            c_inc, c_bs, c_cf = _md2html(raw), "", ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("  %s AI 判讀失敗：%s", stock, exc)

    seg_pct = None
    if seg and seg.get("items"):
        total = sum(i["value"] for i in seg["items"] if i.get("value"))
        if total > 0:
            seg_pct = {"dimension": seg.get("dimension", ""), "period": seg.get("period", ""),
                       "items": [{"label": i["label"], "pct": round(i["value"] / total * 100, 1)}
                                 for i in seg["items"] if i.get("value")]}
    seg_json = json.dumps(seg_pct, ensure_ascii=False) if seg_pct else "null"
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
<p class="sub">產生於 {date.today().isoformat()}・資料源：FinMind／MOPS 財報附註／重訊庫</p>

<h2>四季展望（EPS 預估）</h2>
<table><tr><th>季度</th><th>營收區間(百萬)</th><th>EPS 區間(元)</th></tr>{fc_rows or "<tr><td colspan=3>無月營收資料</td></tr>"}</table>
<p class="note">{model_note}</p>

<h2>訊號</h2><div class="card">{sig_html}</div>

<div class="grid">
<div><h2>營收與毛利率（8 季）</h2><div class="card"><canvas id="revChart" height="210"></canvas></div></div>
<div><h2>營收組成{f"（{seg.get('dimension','')}，{seg.get('period','')}）" if seg and seg.get('items') else ""}</h2>
<div class="card"><canvas id="pieChart" height="210"></canvas>
{"" if seg_pct else f"<p class='note'>財報附註無產品/服務別拆分{('（' + seg['note'] + '）') if seg and seg.get('note') else ''}。</p>"}</div></div>
</div>

<h2>損益表（單季，左起為預估季）</h2>{inc_html}
<p class="note">預估欄＝模型中值：營收取 nowcast 區間中點、毛利率取近 8 季截尾中值、費用用擬合線、業外/稅率用中位；區間見上方四季展望。</p>
<div class="card">{c_inc}</div>

<h2>資產負債關鍵科目（期末，左起為預估季）</h2>{bs_html}
<p class="note">預估欄＝營收比率法：應收/存貨按近 4 季「佔營收比」中位 × 預估營收；其餘科目不預估。</p>
<div class="card">{c_bs}</div>

<h2>現金流量（單季，年內差分還原，左起為預估季）</h2>{cf_html}
<p class="note">預估欄＝近 8 季中位數粗估，僅供量級參考。</p>
<div class="card" style="margin-bottom:14px"><canvas id="cfChart" height="120"></canvas></div>
<div class="card">{c_cf}</div>

<h2>重訊時間軸（近 120 天）</h2>{ann_html}

<script>
const C = {chart_json}, SEG = {seg_json}, CF = {cf_chart};
if (C.labels) new Chart(revChart, {{type:'bar', data:{{labels:C.labels, datasets:[
 {{label:'營收(百萬)', data:C.rev, backgroundColor:'#AFA9EC', yAxisID:'y'}},
 {{label:'毛利率%', data:C.gm, type:'line', borderColor:'#D85A30', yAxisID:'y2', tension:.2}}]}},
 options:{{scales:{{y:{{position:'left'}}, y2:{{position:'right', grid:{{display:false}}}}}}}}}});
if (SEG) new Chart(pieChart, {{type:'doughnut', data:{{labels:SEG.items.map(i=>`${{i.label}} ${{i.pct}}%`),
 datasets:[{{data:SEG.items.map(i=>i.pct), backgroundColor:['#534AB7','#1D9E75','#D85A30','#D4537E','#378ADD','#BA7517','#888780','#97C459']}}]}},
 options:{{plugins:{{legend:{{position:'right'}}, tooltip:{{callbacks:{{label:(c)=>c.label}}}}}}}}}});
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
            "mae": pl.get("mae") if pl else None, "volatile": bool(pl and pl.get("volatile"))}


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
        eps_s = _mid_rng(r["eps"], 1, 2) + (" ⚠" if r["volatile"] else "")
        mae_s = f"{r['mae']:.2f}" if r["mae"] is not None else "—"
        trs += (f"<tr><td><a href='{r['stock']}.html'>{r['stock']} {r['name']}</a></td>"
                f"<td>{r['quarter']}</td><td>{_fmt(r['rev'], 1e3)}</td><td>{eps_s}</td>"
                f"<td>{_mid_rng(r['eps_n1'], 1, 2)}</td>"
                f"<td>{mae_s}</td></tr>")
    html = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>公司檔案總覽</title><style>{_CSS}</style></head><body><div class="wrap">
<h1>公司檔案總覽</h1><p class="sub">產生於 {date.today().isoformat()}</p>
<table><tr><th>公司</th><th>T 季度</th><th>T 營收(百萬)</th><th>T EPS 區間</th><th>T+1 EPS 區間</th><th>回測 MAE</th></tr>{trs}</table>
</div></body></html>"""
    (cfg.data_dir / "dashboard" / "index.html").write_text(html, encoding="utf-8")
    logger.info("dashboard 完成 %d 檔 → %s", len(rows), cfg.data_dir / "dashboard")
    return 0
