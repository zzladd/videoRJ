"""API 请求/响应模型与执行脚本 JSON Schema。"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# 执行脚本 JSON（结构化）—— 系统的核心契约
# ---------------------------------------------------------------------------


class SubtitleSpec(BaseModel):
    mode: Literal["burn", "soft", "none"] = "burn"  # 烧录 / 软字幕 / 无
    font_size: int = 16


class ShotSpec(BaseModel):
    """单个镜头：narration 作为字幕文案，keywords 用于素材匹配。"""

    index: int
    narration: str = ""
    keywords: list[str] = Field(default_factory=list)
    duration: float = Field(gt=0, le=60, default=3.0)
    material_id: str | None = None  # 指定素材（可选），否则自动匹配
    transition: Literal["cut", "fade"] = "cut"


class ExecutionScript(BaseModel):
    """结构化执行脚本。LLM 输出经此校验后才能进入渲染。"""

    version: int = 1
    title: str = ""
    width: int = 1080
    height: int = 1920
    fps: int = 30
    keep_source_audio: bool = False  # 是否保留素材原声
    bgm_path: str | None = None
    subtitle: SubtitleSpec = Field(default_factory=SubtitleSpec)
    shots: list[ShotSpec] = Field(min_length=1)

    @field_validator("shots")
    @classmethod
    def _reindex(cls, shots: list[ShotSpec]) -> list[ShotSpec]:
        for i, s in enumerate(shots):
            s.index = i + 1
        return shots

    @property
    def total_duration(self) -> float:
        return sum(s.duration for s in self.shots)


# ---------------------------------------------------------------------------
# API 模型
# ---------------------------------------------------------------------------


class MaterialOut(BaseModel):
    id: str
    title: str
    source_type: str
    source_url: str
    license_note: str
    tags: list[str]
    duration: float
    width: int
    height: int
    status: str
    error: str
    cover_url: str | None = None
    preview_url: str | None = None
    segment_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class SegmentOut(BaseModel):
    id: str
    material_id: str
    start: float
    end: float
    asr_text: str
    ocr_text: str
    keywords: list[str]

    model_config = {"from_attributes": True}


class MaterialFromUrlIn(BaseModel):
    url: str
    title: str = ""
    license_note: str = ""  # 授权说明，建议填写授权方与授权范围
    tags: list[str] = Field(default_factory=list)


class ScriptGenerateIn(BaseModel):
    topic: str  # 主题 / 创作要求
    style: str = "口播带货"  # 风格提示
    target_duration: float = Field(default=30.0, gt=5, le=300)
    extra_requirements: str = ""


class ScriptOut(BaseModel):
    id: str
    title: str
    topic: str
    content: str
    execution: dict[str, Any] | None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ScriptUpdateIn(BaseModel):
    title: str | None = None
    content: str | None = None
    execution: dict[str, Any] | None = None


class RenderOptionsIn(BaseModel):
    """提交渲染时的可覆盖选项。"""

    width: int | None = None
    height: int | None = None
    subtitle_mode: Literal["burn", "soft", "none"] | None = None
    keep_source_audio: bool | None = None
    material_ids: list[str] | None = None  # 限定素材池（可选）


class RenderJobOut(BaseModel):
    id: str
    script_id: str
    status: str
    progress: float
    message: str
    timeline: list[Any] | None
    output_url: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
