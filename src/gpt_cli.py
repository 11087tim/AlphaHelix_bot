"""共用 ChatGPT 訂閱後端：以 OpenAI Codex CLI headless（codex exec）呼叫 GPT-5.6 系列。

取代 OpenRouter API 計費——用量計入 ChatGPT 訂閱額度。認證：在有訂閱登入的機器跑
`codex login`（OAuth），token 快取於 ~/.codex/auth.json（VM 部署可從本機 scp 該檔過去，
CLI 會自動刷新）。CLI 不在 PATH 時以 CODEX_BIN 指定路徑。

模型分級（對應原 Claude 級距）：
  gpt-5.6-sol   強模型（原 Opus 級；reasoning effort 預設 high，可用 GPT_SOL_EFFORT 覆蓋）
  gpt-5.6-terra 中模型（原 Sonnet 級）
  gpt-5.6-luna  輕模型（原 Haiku 級）

限制：codex exec 不回傳 token 用量，cost 欄位一律回 0.0（訂閱內不另計費）。
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
_IMG_DL_TIMEOUT = 30   # 單張推文圖片下載逾時
# sol 走高推理是本次遷移的明確要求；terra/luna 用 codex 預設 effort
_EFFORT = {"gpt-5.6-sol": os.environ.get("GPT_SOL_EFFORT", "high")}


def find_codex_bin() -> str:
    """找 codex CLI 執行檔：CODEX_BIN 環境變數優先，其次 PATH。找不到即報錯。"""
    bin_path = os.environ.get("CODEX_BIN") or shutil.which("codex")
    if not bin_path:
        raise RuntimeError(
            "找不到 codex CLI。請安裝（npm install -g @openai/codex）並以 ChatGPT 訂閱"
            "帳號 `codex login`（headless 機器可從已登入機器複製 ~/.codex/auth.json）；"
            "CLI 不在 PATH 時以 CODEX_BIN 指定路徑。")
    return bin_path


def _materialize_image(entry: str, tmpdir: Path, idx: int) -> Path | None:
    """把一張圖（http URL 或 data:base64 URI）落成暫存檔供 -i 附加。失敗回 None（略過該圖）。"""
    try:
        if entry.startswith("data:"):
            header, b64 = entry.split(",", 1)
            ext = ".png" if "png" in header else ".jpg"
            path = tmpdir / f"img{idx}{ext}"
            path.write_bytes(base64.b64decode(b64))
            return path
        resp = requests.get(entry, timeout=_IMG_DL_TIMEOUT)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        ext = ".png" if "png" in ctype else ".webp" if "webp" in ctype else ".gif" if "gif" in ctype else ".jpg"
        path = tmpdir / f"img{idx}{ext}"
        path.write_bytes(resp.content)
        return path
    except Exception as exc:  # noqa: BLE001 圖片取不到不該讓整次呼叫失敗
        logger.warning("圖片取得失敗，略過（%s）：%s", entry[:80], exc)
        return None


def _run(model: str, prompt: str, image_paths: list[Path], timeout: int) -> str:
    """跑一次 codex exec，回傳最終訊息文字（stdout）。含重試與退避。"""
    cmd = [find_codex_bin(), "exec",
           "--model", model,
           "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral"]
    effort = _EFFORT.get(model)
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    cmd.append(prompt)
    if image_paths:  # -i 需接在 prompt 之後
        cmd += ["-i", ",".join(str(p) for p in image_paths)]
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, cwd=tempfile.gettempdir())
            if proc.returncode != 0:
                raise RuntimeError(f"codex exec 退出碼 {proc.returncode}："
                                   f"{(proc.stderr or proc.stdout).strip()[:500]}")
            out = proc.stdout.strip()
            if not out:
                raise RuntimeError("codex exec 未回傳任何內容")
            return out
        except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                break
            wait = min(2 ** attempt * 5, 60)
            logger.warning("codex 呼叫失敗（第 %d/%d 次）：%s；%d 秒後重試。",
                           attempt, MAX_RETRIES, exc, wait)
            time.sleep(wait)
    raise last_exc  # type: ignore[misc]


def _flatten(payload: dict) -> tuple[str, str, list[str]]:
    """把 OpenRouter chat-completions 形狀的 payload 拆成（model, 合併 prompt, 圖片清單）。
    system 併入 prompt 開頭（codex exec 無獨立 system 欄位）。"""
    model = payload["model"]
    parts: list[str] = []
    images: list[str] = []
    for msg in payload.get("messages", []):
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        for item in content or []:  # 多模態 list：text 與 image_url 混排
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "image_url":
                url = (item.get("image_url") or {}).get("url", "")
                if url:
                    images.append(url)
    return model, "\n\n".join(p for p in parts if p), images


def chat_payload(payload: dict, timeout: int = 900) -> dict:
    """OpenRouter 相容入口（供 summarizer._post_chat 轉呼叫）：
    收 chat-completions payload，回傳 {"choices":[{"message":{"content": ...}}]}。"""
    model, prompt, image_entries = _flatten(payload)
    with tempfile.TemporaryDirectory(prefix="gptcli_") as td:
        paths = [p for i, e in enumerate(image_entries)
                 if (p := _materialize_image(e, Path(td), i))]
        text = _run(model, prompt, paths, timeout)
    return {"choices": [{"message": {"content": text}}]}


def chat(model: str, system: str, user: str, timeout: int = 600) -> dict:
    """reports/llm.chat 相容入口。回傳 {text, prompt_tokens, completion_tokens, cost}。"""
    text = _run(model, f"{system}\n\n{user}", [], timeout)
    return {"text": text, "prompt_tokens": None, "completion_tokens": None, "cost": 0.0}


def vision_chat(model: str, system: str, user: str, images_b64: list[str],
                timeout: int = 900) -> dict:
    """reports/llm.vision_chat 相容入口：images_b64 為 base64 PNG 清單。"""
    with tempfile.TemporaryDirectory(prefix="gptcli_") as td:
        paths = [p for i, b64 in enumerate(images_b64)
                 if (p := _materialize_image(f"data:image/png;base64,{b64}", Path(td), i))]
        text = _run(model, f"{system}\n\n{user}", paths, timeout)
    return {"text": text, "prompt_tokens": None, "completion_tokens": None, "cost": 0.0}


def backend() -> str:
    """目前生效的 LLM 後端：codex（預設）或 openrouter（緊急回退，設 XBOT_LLM_BACKEND）。"""
    return os.environ.get("XBOT_LLM_BACKEND", "codex").strip().lower()


def openrouter_slug(model: str) -> str:
    """回退 OpenRouter 時把裸 GPT 代號補上供應商前綴（gpt-5.6-sol → openai/gpt-5.6-sol），
    讓一鍵回退不需改動任何模型配置。"""
    return f"openai/{model}" if model.startswith("gpt-") else model
