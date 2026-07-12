"""音訊轉文字：下載 mp3 → ffmpeg 降頻(16kHz 單聲道)並切塊 → Groq Whisper 逐塊轉錄 → 併回逐字稿。

切塊是為了穩過 Whisper API 的檔案大小上限（不論節目多長都適用）。
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
SEGMENT_SEC = 600  # 每塊 10 分鐘（16kHz 單聲道 64kbps 約 4.8MB，穩過上限）


def _download(url: str, dest: Path) -> None:
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "Mozilla/5.0"}) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)


def _segment(src: Path, outdir: Path) -> list[Path]:
    """降頻成 16kHz 單聲道並切成固定長度小塊，回傳塊檔清單（依序）。"""
    pattern = str(outdir / "chunk_%03d.mp3")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src),
        "-ar", "16000", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k",
        "-f", "segment", "-segment_time", str(SEGMENT_SEC), pattern,
    ]
    subprocess.run(cmd, check=True)
    return sorted(outdir.glob("chunk_*.mp3"))


def _transcribe_chunk(path: Path, api_key: str, model: str) -> str:
    with open(path, "rb") as f:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (path.name, f, "audio/mpeg")},
            data={"model": model, "response_format": "json"},  # 不指定語言→Whisper 自動偵測（中英皆可）
            timeout=300,
        )
    resp.raise_for_status()
    return (resp.json().get("text") or "").strip()


def transcribe_url(audio_url: str, api_key: str, model: str) -> str:
    """下載並轉錄一個音訊網址，回傳完整逐字稿。失敗會丟出例外由呼叫端處理。"""
    with tempfile.TemporaryDirectory(prefix="podcast_") as tmp:
        tmpdir = Path(tmp)
        mp3 = tmpdir / "audio.mp3"
        logger.info("下載音檔…")
        _download(audio_url, mp3)
        logger.info("ffmpeg 降頻切塊…")
        chunks = _segment(mp3, tmpdir)
        logger.info("Whisper 轉錄 %d 塊…", len(chunks))
        parts = []
        for i, ch in enumerate(chunks, 1):
            parts.append(_transcribe_chunk(ch, api_key, model))
            logger.info("  第 %d/%d 塊完成", i, len(chunks))
        return "\n".join(p for p in parts if p).strip()
