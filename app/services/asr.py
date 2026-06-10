"""ASR 提供方（可插拔）。

- none:           跳过转写（无语音素材或暂不需要）
- faster_whisper: 本地推理（pip install faster-whisper），数据不出本机
- openai:         OpenAI 兼容 /audio/transcriptions 接口
"""
import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class AsrLine:
    start: float
    end: float
    text: str


def transcribe(wav_path: str | Path) -> list[AsrLine]:
    provider = get_settings().asr_provider
    if provider == "none":
        return []
    if provider == "faster_whisper":
        return _faster_whisper(wav_path)
    if provider == "openai":
        return _openai_api(wav_path)
    raise ValueError(f"未知 ASR provider: {provider}")


def _faster_whisper(wav_path: str | Path) -> list[AsrLine]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError("请先安装: pip install faster-whisper") from e

    settings = get_settings()
    model = WhisperModel(settings.asr_whisper_model, device="auto", compute_type="auto")
    segments, _info = model.transcribe(str(wav_path), language="zh", vad_filter=True)
    return [AsrLine(start=seg.start, end=seg.end, text=seg.text.strip()) for seg in segments if seg.text.strip()]


def _openai_api(wav_path: str | Path) -> list[AsrLine]:
    import httpx

    settings = get_settings()
    url = settings.asr_api_base_url.rstrip("/") + "/audio/transcriptions"
    with open(wav_path, "rb") as f:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {settings.asr_api_key}"},
            files={"file": (Path(wav_path).name, f, "audio/wav")},
            data={"model": settings.asr_api_model, "response_format": "verbose_json"},
            timeout=600,
        )
    resp.raise_for_status()
    data = resp.json()
    lines = [
        AsrLine(start=float(seg["start"]), end=float(seg["end"]), text=seg["text"].strip())
        for seg in data.get("segments", [])
        if seg.get("text", "").strip()
    ]
    if not lines and data.get("text"):
        lines = [AsrLine(start=0.0, end=0.0, text=data["text"].strip())]
    return lines
