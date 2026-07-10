from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def load_graph_context() -> str | None:
    """載入關係圖並整理成 LLM 參考文字；沒有 graph.yaml 時回傳 None（功能可選）。"""
    try:
        from graph.model import load_graph

        return load_graph().to_prompt_context()
    except Exception as exc:  # noqa: BLE001
        logger.info("未載入關係圖（Opus 判讀時不附產業圖）：%s", exc)
        return None
