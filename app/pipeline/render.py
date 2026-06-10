"""渲染流水线（异步任务入口）：素材匹配 -> 混剪合成 -> 成片落盘。"""
import logging

from app.config import get_settings
from app.database import SessionLocal
from app.models import Material, RenderJob, Script
from app.schemas import ExecutionScript
from app.services.composer import compose
from app.services.matcher import build_auto_timeline, match_script

logger = logging.getLogger(__name__)


def run_render_job(job_id: str) -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        job = db.get(RenderJob, job_id)
        if job is None:
            logger.error("渲染任务不存在: %s", job_id)
            return
        try:
            opts = job.options or {}

            # 1. 生成时间线：有脚本 = AI 匹配；无脚本 = 所选素材轮换自动混剪
            job.status = "matching"
            job.progress = 0.05
            job.message = "正在匹配素材片段"
            db.commit()

            if job.script_id:
                script_row = db.get(Script, job.script_id)
                if script_row is None or not script_row.execution:
                    raise ValueError("脚本不存在或尚未生成执行脚本")
                spec = ExecutionScript.model_validate(script_row.execution)
                if opts.get("width"):
                    spec.width = int(opts["width"])
                if opts.get("height"):
                    spec.height = int(opts["height"])
                if opts.get("subtitle_mode"):
                    spec.subtitle.mode = opts["subtitle_mode"]
                if opts.get("keep_source_audio") is not None:
                    spec.keep_source_audio = bool(opts["keep_source_audio"])
                timeline = match_script(db, spec, job.tenant_id, opts.get("material_ids"))
                render_params = dict(
                    width=spec.width, height=spec.height, fps=spec.fps,
                    keep_source_audio=spec.keep_source_audio,
                    bgm_path=spec.bgm_path,
                    subtitle_mode=spec.subtitle.mode,
                    subtitle_font_size=spec.subtitle.font_size,
                )
            else:
                if not opts.get("material_ids"):
                    raise ValueError("自动混剪任务缺少素材列表")
                timeline = build_auto_timeline(
                    db, job.tenant_id, opts["material_ids"],
                    target_duration=float(opts.get("target_duration", 30.0)),
                    clip_duration=float(opts.get("clip_duration", 3.5)),
                )
                render_params = dict(
                    width=int(opts.get("width") or 1080),
                    height=int(opts.get("height") or 1920),
                    fps=30,
                    # 无脚本无字幕，默认保留素材原声，否则成片是无声的
                    keep_source_audio=bool(opts.get("keep_source_audio", True)),
                    bgm_path=None,
                    subtitle_mode=opts.get("subtitle_mode") or "none",
                    subtitle_font_size=16,
                )

            job.timeline = [c.to_dict() for c in timeline]
            db.commit()

            # 2. 合成
            job.status = "rendering"
            job.message = "正在合成视频"
            db.commit()

            material_ids = {c.material_id for c in timeline}
            mats = db.query(Material).filter(Material.id.in_(material_ids)).all()
            material_paths = {m.id: m.video_path for m in mats}

            def on_progress(p: float, msg: str) -> None:
                job.progress = round(p, 3)
                job.message = msg
                db.commit()

            output = settings.outputs_dir / f"{job.id}.mp4"
            compose(timeline, material_paths, output, on_progress=on_progress, **render_params)
            job.output_path = str(output)
            job.status = "success"
            job.progress = 1.0
            job.message = "渲染完成"
            db.commit()
            logger.info("渲染完成: %s -> %s", job.id, output)
        except Exception as e:
            logger.exception("渲染失败: %s", job_id)
            db.rollback()
            job = db.get(RenderJob, job_id)
            if job:
                job.status = "failed"
                job.message = str(e)[:2000]
                db.commit()
    finally:
        db.close()
