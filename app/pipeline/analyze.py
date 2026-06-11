"""素材分析流水线（异步任务入口）。

下载(可选) -> 转码标准化 -> 探测元信息 -> 封面 -> 场景切分 -> 抽帧 -> ASR -> OCR -> 片段入库
任何一步失败都会把 Material 标记为 failed 并记录错误，方便前端展示与重试。
"""
import logging
import shutil
from pathlib import Path

import jieba

from app.config import get_settings
from app.database import SessionLocal
from app.models import Material, Segment, TranscriptLine
from app.services import media
from app.services.asr import transcribe
from app.services.downloader import download_video
from app.services.ocr import ocr_image

logger = logging.getLogger(__name__)


def analyze_material(material_id: str) -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        mat = db.get(Material, material_id)
        if mat is None:
            logger.error("素材不存在: %s", material_id)
            return
        try:
            # 0. 链接下载
            if mat.source_type == "url" and not mat.original_path:
                mat.status = "downloading"
                db.commit()
                raw_path, title = download_video(mat.source_url, settings.materials_dir, mat.id)
                mat.original_path = str(raw_path)
                if title and not mat.title:
                    mat.title = title
            mat.status = "analyzing"
            db.commit()

            src = Path(mat.original_path)
            if not src.exists():
                raise FileNotFoundError(f"原始文件不存在: {src}")

            # 1. 转码标准化
            std_path = settings.materials_dir / f"{mat.id}.mp4"
            media.transcode_standard(src, std_path)
            mat.video_path = str(std_path)

            # 2. 元信息
            info = media.probe(std_path)
            mat.duration, mat.width, mat.height, mat.fps = (
                info.duration, info.width, info.height, round(info.fps, 2),
            )

            # 3. 封面
            cover = settings.covers_dir / f"{mat.id}.jpg"
            media.extract_cover(std_path, cover, at=min(1.0, info.duration / 3))
            mat.cover_path = str(cover)
            db.commit()

            # 4. 场景切分
            scenes = media.detect_scenes(std_path, info.duration)

            # 5. 抽帧（用于 OCR 与片段代表帧）
            frames_dir = settings.frames_dir / mat.id
            frames = media.extract_frames(std_path, frames_dir)

            # 6. ASR
            asr_lines = []
            if info.has_audio and settings.asr_provider != "none":
                wav = settings.tmp_dir / f"{mat.id}.wav"
                try:
                    media.extract_audio(std_path, wav)
                    asr_lines = transcribe(wav)
                finally:
                    wav.unlink(missing_ok=True)
            db.query(TranscriptLine).filter_by(material_id=mat.id).delete()
            for line in asr_lines:
                db.add(TranscriptLine(material_id=mat.id, start=line.start, end=line.end, text=line.text))

            # 7. OCR（每帧识别，按时间归并到片段）
            frame_texts: list[tuple[float, str]] = []
            if settings.ocr_provider != "none":
                for ts, fp in frames:
                    text = ocr_image(fp)
                    if text:
                        frame_texts.append((ts, text))

            # 8. 片段入库
            db.query(Segment).filter_by(material_id=mat.id).delete()
            for start, end in scenes:
                seg_asr = " ".join(
                    line.text for line in asr_lines if line.start < end and line.end > start
                )
                seg_ocr_parts: list[str] = []
                seen: set[str] = set()
                for ts, text in frame_texts:
                    if start <= ts < end and text not in seen:
                        seen.add(text)
                        seg_ocr_parts.append(text)
                seg_ocr = " ".join(seg_ocr_parts)
                rep_frame = _nearest_frame(frames, (start + end) / 2)
                keywords = _extract_keywords(f"{seg_asr} {seg_ocr} {mat.title}")
                db.add(Segment(
                    material_id=mat.id, start=round(start, 3), end=round(end, 3),
                    asr_text=seg_asr, ocr_text=seg_ocr, keywords=keywords,
                    frame_path=str(rep_frame) if rep_frame else "",
                ))

            mat.status = "ready"
            mat.error = ""
            db.commit()
            logger.info("素材分析完成: %s (%d 个片段)", mat.id, len(scenes))
        except Exception as e:
            logger.exception("素材分析失败: %s", material_id)
            db.rollback()
            mat = db.get(Material, material_id)
            if mat:
                mat.status = "failed"
                mat.error = str(e)[:2000]
                db.commit()
    finally:
        db.close()


def _nearest_frame(frames: list[tuple[float, Path]], ts: float) -> Path | None:
    if not frames:
        return None
    return min(frames, key=lambda f: abs(f[0] - ts))[1]


def _extract_keywords(text: str, top_n: int = 10) -> list[str]:
    """简易关键词抽取：分词去重取高频词。SaaS 化可换 TF-IDF / LLM 打标。"""
    counts: dict[str, int] = {}
    for tok in jieba.cut(text):
        tok = tok.strip()
        if len(tok) >= 2:
            counts[tok] = counts.get(tok, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:top_n]]


def delete_material_files(mat: Material) -> None:
    """删除素材关联的磁盘文件。"""
    settings = get_settings()
    for p in (mat.original_path, mat.video_path, mat.cover_path):
        if p:
            Path(p).unlink(missing_ok=True)
    shutil.rmtree(settings.frames_dir / mat.id, ignore_errors=True)
