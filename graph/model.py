from __future__ import annotations

import re
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = PROJECT_ROOT / "graph.yaml"


class Graph:
    def __init__(self, data: dict):
        self.themes = data.get("themes", []) or []
        self.companies = data.get("companies", {}) or {}
        self.holdings = {str(t) for t in (data.get("holdings") or [])}  # 有實際部位者
        self._normalize()
        self._alias_index = self._build_alias_index()

    def status(self, ticker: str) -> str:
        """hold=有部位、watch=關注中。"""
        return "hold" if ticker in self.holdings else "watch"

    def to_prompt_context(self) -> str:
        """把關係圖整理成給 LLM 參考的精簡文字（供推導供應鏈關聯用）。"""
        lines = ["【產業關係圖（僅供你延伸推導用，不是要你逐條複述）】",
                 "＊主題／子主題 → 相關公司："]
        for t in self.themes:
            for st in t.get("subthemes", []):
                comps = "、".join(st.get("companies") or []) or "—"
                al = f"（別名：{'、'.join(map(str, st.get('aliases') or []))}）" if st.get("aliases") else ""
                lines.append(f"- {t['name']} / {st['name']}{al}：{comps}")
        lines.append("＊公司供應鏈關係（上游供應商｜下游客戶｜競爭對手）：")
        for tk, c in self.companies.items():
            up = "、".join(c.get("upstream") or []) or "—"
            down = "、".join(c.get("downstream") or []) or "—"
            comp = "、".join(c.get("competitors") or []) or "—"
            lines.append(f"- {tk}（{c.get('name', '')}）：上游[{up}]｜下游[{down}]｜競對[{comp}]")
        return "\n".join(lines)

    def mentions(self, text: str) -> tuple[list[str], list[str]]:
        """在一段文字中找出提到的（公司 ticker、主題/子題）。回傳 (tickers, themes)。"""
        low = text.lower()
        tickers: list[str] = []
        for ticker, c in self.companies.items():
            hit = False
            # ticker 用字界比對（避免 TSM 命中 TSMC、MU 命中 much）；允許 $TSM
            if re.search(rf"\$?\b{re.escape(ticker)}\b", text):
                hit = True
            else:  # 公司名/別名用子字串（含中文）
                for a in [c.get("name", "")] + (c.get("aka") or []):
                    a = str(a).strip().lower()
                    if len(a) >= 2 and a in low:
                        hit = True
                        break
            if hit and ticker not in tickers:
                tickers.append(ticker)

        themes: list[str] = []
        for t in self.themes:
            for st in t.get("subthemes", []):
                for a in [st["name"]] + (st.get("aliases") or []):
                    if len(str(a)) >= 2 and str(a).lower() in low:
                        label = f"{t['name']}/{st['name']}"
                        if label not in themes:
                            themes.append(label)
                        break
        return tickers, themes

    def _normalize(self) -> None:
        """把 ticker 一律轉成字串（YAML 會把 5201/2330 這類數字代號解析成 int）。"""
        self.companies = {str(k): v for k, v in self.companies.items()}
        for c in self.companies.values():
            for rel in ("upstream", "downstream", "competitors"):
                if c.get(rel):
                    c[rel] = [str(x) for x in c[rel]]
        for t in self.themes:
            for st in t.get("subthemes", []):
                if st.get("companies"):
                    st["companies"] = [str(x) for x in st["companies"]]

    def _build_alias_index(self) -> dict[str, str]:
        """別名/名稱（正規化）→ ticker，供實體標記用。"""
        idx = {}
        for ticker, c in self.companies.items():
            idx[ticker.lower()] = ticker
            for a in [c.get("name", "")] + (c.get("aka") or []):
                a = str(a).strip()
                if a:
                    idx[a.lower()] = ticker
        return idx

    # ---- 查詢 ----
    def resolve(self, text: str) -> str | None:
        """把一段文字（ticker/公司名/別名）對應到 ticker。"""
        return self._alias_index.get(text.strip().lower())

    def company(self, ticker: str) -> dict | None:
        return self.companies.get(ticker)

    def themes_of(self, ticker: str) -> list[str]:
        """該公司出現在哪些 主題/子題。"""
        out = []
        for t in self.themes:
            for st in t.get("subthemes", []):
                if ticker in (st.get("companies") or []):
                    out.append(f"{t['name']} / {st['name']}")
        return out

    def companies_in(self, name: str) -> list[str]:
        """給主題名或子題名（或別名），回傳相關公司。"""
        n = name.strip().lower()
        found: list[str] = []
        for t in self.themes:
            theme_hit = t["name"].lower() == n
            for st in t.get("subthemes", []):
                aliases = [st["name"]] + (st.get("aliases") or [])
                if theme_hit or any(a.lower() == n for a in aliases):
                    for c in st.get("companies") or []:
                        if c not in found:
                            found.append(c)
        return found

    def neighbors(self, ticker: str) -> dict:
        c = self.companies.get(ticker, {})
        return {
            "upstream": c.get("upstream") or [],
            "downstream": c.get("downstream") or [],
            "competitors": c.get("competitors") or [],
        }

    def check(self) -> tuple[list[str], list[str]]:
        """回傳 (issues, external)。
        issues：主題清單引用了未定義的公司節點（多半是打錯，必修）。
        external：關係邊指向「圖外實體」（如 ASML/Samsung，正常，僅供參考）。"""
        issues, external = [], set()
        known = set(self.companies)
        for t in self.themes:
            for st in t.get("subthemes", []):
                for c in st.get("companies") or []:
                    if c not in known:
                        issues.append(f"主題「{t['name']}/{st['name']}」引用未定義公司：{c}")
        for ticker, c in self.companies.items():
            for rel in ("upstream", "downstream", "competitors"):
                for other in c.get(rel) or []:
                    if other not in known:
                        external.add(other)
        return issues, sorted(external)


def load_graph(path: Path = DEFAULT_PATH) -> Graph:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Graph(data)
