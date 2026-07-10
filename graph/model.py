from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = PROJECT_ROOT / "graph.yaml"


class Graph:
    def __init__(self, data: dict):
        self.themes = data.get("themes", []) or []
        self.companies = data.get("companies", {}) or {}
        self._alias_index = self._build_alias_index()

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
