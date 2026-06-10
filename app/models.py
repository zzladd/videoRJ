"""数据模型。

所有业务表均带 tenant_id 字段，自用时恒为 "default"，
SaaS 化后按租户隔离数据，无需改动表结构。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Material(Base):
    """视频素材（上传或链接下载）。"""

    __tablename__ = "materials"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    source_type: Mapped[str] = mapped_column(String(16), default="upload")  # upload | url
    source_url: Mapped[str] = mapped_column(Text, default="")
    license_note: Mapped[str] = mapped_column(Text, default="")  # 授权说明（合规留痕）
    tags: Mapped[list] = mapped_column(JSON, default=list)

    original_path: Mapped[str] = mapped_column(Text, default="")
    video_path: Mapped[str] = mapped_column(Text, default="")  # 统一转码后的标准 mp4
    cover_path: Mapped[str] = mapped_column(Text, default="")

    duration: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    fps: Mapped[float] = mapped_column(Float, default=0.0)

    # pending | downloading | analyzing | ready | failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    segments: Mapped[list["Segment"]] = relationship(back_populates="material", cascade="all, delete-orphan")
    transcripts: Mapped[list["TranscriptLine"]] = relationship(
        back_populates="material", cascade="all, delete-orphan"
    )


class Segment(Base):
    """场景切分得到的素材片段，是混剪匹配的最小单元。"""

    __tablename__ = "segments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    material_id: Mapped[str] = mapped_column(ForeignKey("materials.id"), index=True)
    start: Mapped[float] = mapped_column(Float, default=0.0)
    end: Mapped[float] = mapped_column(Float, default=0.0)
    asr_text: Mapped[str] = mapped_column(Text, default="")  # 片段时间范围内的语音文本
    ocr_text: Mapped[str] = mapped_column(Text, default="")  # 片段抽帧的画面文字
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    frame_path: Mapped[str] = mapped_column(Text, default="")  # 代表帧缩略图

    material: Mapped[Material] = relationship(back_populates="segments")

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class TranscriptLine(Base):
    """ASR 逐句转写结果（带时间戳），用于字幕保留与全文检索。"""

    __tablename__ = "transcript_lines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    material_id: Mapped[str] = mapped_column(ForeignKey("materials.id"), index=True)
    start: Mapped[float] = mapped_column(Float, default=0.0)
    end: Mapped[float] = mapped_column(Float, default=0.0)
    text: Mapped[str] = mapped_column(Text, default="")

    material: Mapped[Material] = relationship(back_populates="transcripts")


class Script(Base):
    """视频脚本：content 为 AI 生成的内容脚本（文案），execution 为结构化执行脚本 JSON。"""

    __tablename__ = "scripts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    topic: Mapped[str] = mapped_column(Text, default="")  # 用户输入的主题 / 要求
    content: Mapped[str] = mapped_column(Text, default="")  # 内容脚本（口播文案）
    execution: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 执行脚本 JSON
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | ready
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


class RenderJob(Base):
    """异步渲染任务。timeline 记录素材匹配结果，便于追溯与重渲。"""

    __tablename__ = "render_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    script_id: Mapped[str] = mapped_column(ForeignKey("scripts.id"), index=True)
    # queued | matching | rendering | success | failed | canceled
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0 ~ 1
    message: Mapped[str] = mapped_column(Text, default="")
    timeline: Mapped[list | None] = mapped_column(JSON, nullable=True)  # 匹配出的片段时间线
    options: Mapped[dict] = mapped_column(JSON, default=dict)  # 渲染选项（分辨率/字幕模式等）
    output_path: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)
