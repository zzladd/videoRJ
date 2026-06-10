"""素材片段自动匹配：为执行脚本的每个镜头挑选最合适的素材片段。

打分维度：
- 关键词与片段文本（ASR + OCR + 素材标题/标签）的分词重合度
- 片段时长与镜头需求时长的契合度
- 多样性惩罚：避免重复使用同一片段 / 过度集中在同一素材

未来 SaaS 化可替换为向量检索（CLIP 图文嵌入 + 语义向量），接口保持不变。
"""
import logging
from dataclasses import dataclass

import jieba
from sqlalchemy.orm import Session

from app.models import Material, Segment
from app.schemas import ExecutionScript, ShotSpec

logger = logging.getLogger(__name__)


@dataclass
class TimelineClip:
    """时间线上的一个片段（匹配结果）。"""

    shot_index: int
    material_id: str
    segment_id: str | None
    start: float  # 在素材中的起点
    end: float
    narration: str
    transition: str

    def to_dict(self) -> dict:
        return {
            "shot_index": self.shot_index,
            "material_id": self.material_id,
            "segment_id": self.segment_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "narration": self.narration,
            "transition": self.transition,
        }


class MatchError(RuntimeError):
    pass


def _tokenize(text: str) -> set[str]:
    return {t.strip().lower() for t in jieba.cut_for_search(text) if len(t.strip()) >= 2}


def _keyword_score(keywords: list[str], seg_tokens: set[str], seg_text: str) -> float:
    if not keywords:
        return 0.0
    score = 0.0
    text_lower = seg_text.lower()
    for kw in keywords:
        kw = kw.strip().lower()
        if not kw:
            continue
        if kw in text_lower:  # 整词命中权重高
            score += 1.0
            continue
        kw_tokens = _tokenize(kw)
        if kw_tokens and kw_tokens & seg_tokens:
            score += 0.5
    return score / len(keywords)


def _duration_score(seg_duration: float, need: float) -> float:
    if seg_duration <= 0:
        return 0.0
    if seg_duration >= need:
        return 1.0  # 够长即可裁剪
    return max(0.0, seg_duration / need)  # 不够长则按比例扣分


def match_script(
    db: Session,
    script: ExecutionScript,
    tenant_id: str,
    material_ids: list[str] | None = None,
) -> list[TimelineClip]:
    """为每个镜头匹配素材片段，返回完整时间线。"""
    query = (
        db.query(Segment, Material)
        .join(Material, Segment.material_id == Material.id)
        .filter(Material.tenant_id == tenant_id, Material.status == "ready")
    )
    if material_ids:
        query = query.filter(Material.id.in_(material_ids))
    rows = query.all()
    if not rows:
        raise MatchError("没有可用的已分析素材，请先上传并等待素材分析完成")

    # 预计算片段文本与分词
    pool = []
    for seg, mat in rows:
        text = " ".join(filter(None, [seg.asr_text, seg.ocr_text, mat.title, " ".join(mat.tags or [])]))
        pool.append({
            "seg": seg,
            "mat": mat,
            "text": text,
            "tokens": _tokenize(text),
        })

    used_segments: dict[str, int] = {}
    used_materials: dict[str, int] = {}
    timeline: list[TimelineClip] = []

    for shot in script.shots:
        clip = _match_shot(shot, pool, used_segments, used_materials)
        used_segments[clip.segment_id or ""] = used_segments.get(clip.segment_id or "", 0) + 1
        used_materials[clip.material_id] = used_materials.get(clip.material_id, 0) + 1
        timeline.append(clip)
    return timeline


def _match_shot(
    shot: ShotSpec,
    pool: list[dict],
    used_segments: dict[str, int],
    used_materials: dict[str, int],
) -> TimelineClip:
    candidates = pool
    if shot.material_id:  # 用户在执行脚本中指定了素材
        forced = [p for p in pool if p["mat"].id == shot.material_id]
        if forced:
            candidates = forced

    best, best_score = None, float("-inf")
    for p in candidates:
        seg: Segment = p["seg"]
        score = (
            2.0 * _keyword_score(shot.keywords, p["tokens"], p["text"])
            + 1.0 * _duration_score(seg.duration, shot.duration)
            - 1.5 * used_segments.get(seg.id, 0)       # 重复用同一片段重罚
            - 0.3 * used_materials.get(seg.material_id, 0)  # 同素材轻罚，鼓励多样性
        )
        if score > best_score:
            best, best_score = p, score

    assert best is not None
    seg = best["seg"]
    # 在片段内取需求时长；不够长就用整段（合成时会自动适配）
    end = min(seg.end, seg.start + shot.duration)
    if end - seg.start < 0.5:
        end = seg.end
    return TimelineClip(
        shot_index=shot.index,
        material_id=seg.material_id,
        segment_id=seg.id,
        start=seg.start,
        end=end,
        narration=shot.narration,
        transition=shot.transition,
    )
