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
    transition_duration: float = 0.4

    def to_dict(self) -> dict:
        return {
            "shot_index": self.shot_index,
            "material_id": self.material_id,
            "segment_id": self.segment_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "narration": self.narration,
            "transition": self.transition,
            "transition_duration": self.transition_duration,
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

    used_segments: dict[str, int] = {}        # 片段 -> 已使用次数
    consumed: dict[str, float] = {}           # 片段 -> 已消费到的时间点（分窗口复用，避免画面重复）
    used_materials: dict[str, int] = {}
    timeline: list[TimelineClip] = []
    prev_material: str | None = None

    for shot in script.shots:
        clip = _match_shot(shot, pool, used_segments, consumed, used_materials, prev_material)
        if clip.segment_id:
            used_segments[clip.segment_id] = used_segments.get(clip.segment_id, 0) + 1
            consumed[clip.segment_id] = clip.end
        used_materials[clip.material_id] = used_materials.get(clip.material_id, 0) + 1
        prev_material = clip.material_id
        timeline.append(clip)
    return timeline


def _match_shot(
    shot: ShotSpec,
    pool: list[dict],
    used_segments: dict[str, int],
    consumed: dict[str, float],
    used_materials: dict[str, int],
    prev_material: str | None,
) -> TimelineClip:
    candidates = pool
    if shot.material_id:  # 用户在执行脚本中指定了素材
        forced = [p for p in pool if p["mat"].id == shot.material_id]
        if forced:
            candidates = forced

    best, best_score, best_window = None, float("-inf"), (0.0, 0.0)
    for p in candidates:
        seg: Segment = p["seg"]
        # 优先取片段中尚未使用的时间窗口（同片段复用时不重复画面）
        avail_start = max(seg.start, consumed.get(seg.id, seg.start))
        remaining = seg.end - avail_start
        if remaining >= max(1.0, 0.6 * shot.duration):
            window_start = avail_start
            window_len = remaining
            reuse_penalty = 0.2 * used_segments.get(seg.id, 0)  # 不重叠复用，轻罚
        else:
            window_start = seg.start
            window_len = seg.duration
            reuse_penalty = 1.5 * used_segments.get(seg.id, 0)  # 画面重复，重罚

        score = (
            2.0 * _keyword_score(shot.keywords, p["tokens"], p["text"])
            + 1.0 * _duration_score(window_len, shot.duration)
            - reuse_penalty
            - 0.3 * used_materials.get(seg.material_id, 0)  # 总量均衡，鼓励多素材混剪
        )
        # 相邻镜头来自不同素材：交叉混剪的关键（单素材时所有候选同罚，不影响结果）
        if prev_material and seg.material_id == prev_material:
            score -= 0.8
        if score > best_score:
            best, best_score, best_window = p, score, (window_start, window_len)

    assert best is not None
    seg = best["seg"]
    start = best_window[0]
    end = min(seg.end, start + shot.duration)
    if end - start < 0.5:  # 窗口过短则退回整段
        start, end = seg.start, seg.end
    return TimelineClip(
        shot_index=shot.index,
        material_id=seg.material_id,
        segment_id=seg.id,
        start=start,
        end=end,
        narration=shot.narration,
        transition=shot.transition,
        transition_duration=shot.transition_duration,
    )


def build_auto_timeline(
    db: Session,
    tenant_id: str,
    material_ids: list[str],
    target_duration: float = 30.0,
    clip_duration: float = 3.5,
    transition: str = "dissolve",
    transition_duration: float = 0.4,
) -> list[TimelineClip]:
    """无脚本自动混剪：在所选素材间轮流取片段，直到凑够目标时长。

    片段按素材轮换（round-robin）保证交叉混剪；每个片段内部按时间窗口
    顺序消费，画面不重复；素材全部耗尽则提前结束。
    """
    rows = (
        db.query(Segment, Material)
        .join(Material, Segment.material_id == Material.id)
        .filter(
            Material.tenant_id == tenant_id,
            Material.status == "ready",
            Material.id.in_(material_ids),
        )
        .order_by(Segment.start)
        .all()
    )
    if not rows:
        raise MatchError("所选素材中没有可用片段，请确认素材已分析完成")

    # 每个素材一个片段队列（保持素材内时间顺序）
    queues: dict[str, list[Segment]] = {}
    for seg, mat in rows:
        queues.setdefault(mat.id, []).append(seg)
    # 按用户选择顺序轮换
    order = [mid for mid in material_ids if mid in queues]

    offsets: dict[str, float] = {}  # segment_id -> 已消费到的时间点
    timeline: list[TimelineClip] = []
    effective = 0.0  # 计入转场重叠后的成片时长
    shot = 1
    exhausted: set[str] = set()

    while effective < target_duration and len(exhausted) < len(order):
        for mid in order:
            if effective >= target_duration or mid in exhausted:
                continue
            clip = _next_window(queues[mid], offsets, clip_duration)
            if clip is None:
                exhausted.add(mid)
                continue
            start, end, seg = clip
            trans = "fade" if shot == 1 else transition
            overlap = 0.0 if shot == 1 else min(transition_duration, end - start)
            timeline.append(TimelineClip(
                shot_index=shot,
                material_id=mid,
                segment_id=seg.id,
                start=start,
                end=end,
                narration="",
                transition=trans,
                transition_duration=transition_duration,
            ))
            effective += (end - start) - overlap
            shot += 1
    if not timeline:
        raise MatchError("所选素材时长不足，无法生成时间线")
    return timeline


def _next_window(
    segments: list[Segment], offsets: dict[str, float], clip_duration: float,
) -> tuple[float, float, Segment] | None:
    """取该素材下一个未消费的时间窗口；全部耗尽返回 None。"""
    for seg in segments:
        cursor = offsets.get(seg.id, seg.start)
        remaining = seg.end - cursor
        if remaining < 1.0:
            continue
        end = min(seg.end, cursor + clip_duration)
        offsets[seg.id] = end
        return cursor, end, seg
    return None
