"""OCR 提供方（可插拔）。

- none:     跳过
- rapidocr: 本地 ONNX 推理（pip install rapidocr-onnxruntime），中英文效果好且无需 GPU
"""
import logging
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


def ocr_image(image_path: str | Path) -> str:
    provider = get_settings().ocr_provider
    if provider == "none":
        return ""
    if provider == "rapidocr":
        return _rapidocr(image_path)
    raise ValueError(f"未知 OCR provider: {provider}")


@lru_cache(maxsize=1)
def _get_rapidocr_engine():
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as e:
        raise RuntimeError("请先安装: pip install rapidocr-onnxruntime") from e
    return RapidOCR()


def _rapidocr(image_path: str | Path) -> str:
    engine = _get_rapidocr_engine()
    result, _ = engine(str(image_path))
    if not result:
        return ""
    return " ".join(item[1] for item in result if len(item) > 1 and item[1])
