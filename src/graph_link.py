from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def compute_relevance(sections: list[dict]) -> dict | None:
    """從彙整內容找出與關係圖重疊的公司/主題。
    回傳 {"companies": [{ticker, name, status}], "themes": [...]}；
    若沒有 graph.yaml 或載入失敗則回傳 None（功能可選、不影響主流程）。"""
    try:
        from graph.model import load_graph

        g = load_graph()
    except Exception as exc:  # noqa: BLE001
        logger.info("未載入關係圖（略過關聯標記）：%s", exc)
        return None

    text = "\n".join(s.get("summary", "") for s in sections)
    tickers, themes = g.mentions(text)
    companies = [
        {"ticker": t, "name": (g.company(t) or {}).get("name", t)}
        for t in tickers
    ]
    return {"companies": companies, "themes": themes}
