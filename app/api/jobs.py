"""渲染任务 API：提交渲染、查询进度、成片预览与下载。"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth import get_tenant
from app.database import get_db
from app.models import Material, RenderJob, Script
from app.schemas import DirectRenderIn, RenderJobOut, RenderOptionsIn, TimelineRenderIn
from app.tasks.runner import dispatch

router = APIRouter(prefix="/api", tags=["渲染任务"])


def _to_out(job: RenderJob) -> RenderJobOut:
    out = RenderJobOut.model_validate(job)
    if job.status == "success" and job.output_path:
        out.output_url = f"/api/jobs/{job.id}/output"
    if job.script_id:
        out.kind = "script"
    elif (job.options or {}).get("custom_timeline"):
        out.kind = "manual"
    else:
        out.kind = "auto"
    return out


@router.post("/render", response_model=RenderJobOut)
def submit_direct_render(
    body: DirectRenderIn,
    tenant: str = Depends(get_tenant),
    db: Session = Depends(get_db),
):
    """直接渲染：手动选择多个素材 + 一个脚本（可选）。

    - 选了脚本：在所选素材范围内按脚本关键词智能匹配片段
    - 不选脚本：自动混剪——所选素材轮流取片段、叠化转场、保留原声
    """
    wanted = list(dict.fromkeys(body.material_ids))  # 去重保序
    mats = (
        db.query(Material)
        .filter(Material.id.in_(wanted), Material.tenant_id == tenant)
        .all()
    )
    found = {m.id: m for m in mats}
    missing = [mid for mid in wanted if mid not in found]
    if missing:
        raise HTTPException(404, f"素材不存在: {missing}")
    not_ready = [m.title or m.id for m in mats if m.status != "ready"]
    if not_ready:
        raise HTTPException(400, f"素材尚未分析完成: {not_ready}")

    if body.script_id:
        script = db.get(Script, body.script_id)
        if script is None or script.tenant_id != tenant:
            raise HTTPException(404, "脚本不存在")
        if not script.execution:
            raise HTTPException(400, "脚本尚未生成执行脚本 JSON")

    options = {
        "material_ids": wanted,
        "target_duration": body.target_duration,
        "clip_duration": body.clip_duration,
    }
    for key in ("width", "height", "subtitle_mode", "keep_source_audio"):
        value = getattr(body, key)
        if value is not None:
            options[key] = value

    job = RenderJob(tenant_id=tenant, script_id=body.script_id or "", options=options)
    db.add(job)
    db.commit()
    dispatch("render_job", job.id)
    return _to_out(job)


@router.post("/render/timeline", response_model=RenderJobOut)
def submit_timeline_render(
    body: TimelineRenderIn,
    tenant: str = Depends(get_tenant),
    db: Session = Depends(get_db),
):
    """手动剪辑渲染：按剪辑台拖拽编排的时间线直接合成（不经过匹配）。"""
    wanted = list({c.material_id for c in body.clips})
    mats = (
        db.query(Material)
        .filter(Material.id.in_(wanted), Material.tenant_id == tenant)
        .all()
    )
    found = {m.id: m for m in mats}
    missing = [mid for mid in wanted if mid not in found]
    if missing:
        raise HTTPException(404, f"素材不存在: {missing}")
    not_ready = [m.title or m.id for m in mats if m.status != "ready"]
    if not_ready:
        raise HTTPException(400, f"素材尚未分析完成: {not_ready}")
    for i, c in enumerate(body.clips):
        mat = found[c.material_id]
        if c.end <= c.start:
            raise HTTPException(400, f"第 {i + 1} 段起止时间无效: {c.start} - {c.end}")
        if c.start >= mat.duration:
            raise HTTPException(400, f"第 {i + 1} 段起点超出素材时长（{mat.duration:.1f}s）")

    options = {
        "custom_timeline": [c.model_dump() for c in body.clips],
        "width": body.width,
        "height": body.height,
        "keep_source_audio": body.keep_source_audio,
        "subtitle_mode": body.subtitle_mode,
    }
    job = RenderJob(tenant_id=tenant, script_id="", options=options)
    db.add(job)
    db.commit()
    dispatch("render_job", job.id)
    return _to_out(job)


@router.post("/scripts/{script_id}/render", response_model=RenderJobOut)
def submit_render(
    script_id: str,
    body: RenderOptionsIn | None = None,
    tenant: str = Depends(get_tenant),
    db: Session = Depends(get_db),
):
    """提交渲染任务（异步执行，轮询 /api/jobs/{id} 获取进度）。"""
    script = db.get(Script, script_id)
    if script is None or script.tenant_id != tenant:
        raise HTTPException(404, "脚本不存在")
    if not script.execution:
        raise HTTPException(400, "脚本尚未生成执行脚本 JSON")

    options = body.model_dump(exclude_none=True) if body else {}
    job = RenderJob(tenant_id=tenant, script_id=script_id, options=options)
    db.add(job)
    db.commit()
    dispatch("render_job", job.id)
    return _to_out(job)


@router.get("/jobs", response_model=list[RenderJobOut])
def list_jobs(
    status: str | None = None,
    tenant: str = Depends(get_tenant),
    db: Session = Depends(get_db),
):
    q = db.query(RenderJob).filter_by(tenant_id=tenant)
    if status:
        q = q.filter_by(status=status)
    return [_to_out(j) for j in q.order_by(RenderJob.created_at.desc()).all()]


def _get_job(job_id: str, tenant: str, db: Session) -> RenderJob:
    job = db.get(RenderJob, job_id)
    if job is None or job.tenant_id != tenant:
        raise HTTPException(404, "任务不存在")
    return job


@router.get("/jobs/{job_id}", response_model=RenderJobOut)
def get_job(job_id: str, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    return _to_out(_get_job(job_id, tenant, db))


@router.post("/jobs/{job_id}/retry", response_model=RenderJobOut)
def retry_job(job_id: str, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    job = _get_job(job_id, tenant, db)
    if job.status not in ("failed", "canceled"):
        raise HTTPException(400, f"当前状态不可重试: {job.status}")
    job.status = "queued"
    job.progress = 0.0
    job.message = ""
    db.commit()
    dispatch("render_job", job.id)
    return _to_out(job)


@router.get("/jobs/{job_id}/output")
def download_output(
    job_id: str,
    download: bool = False,
    tenant: str = Depends(get_tenant),
    db: Session = Depends(get_db),
):
    """成片预览（默认内联播放）或下载（?download=true）。"""
    job = _get_job(job_id, tenant, db)
    if job.status != "success" or not job.output_path or not Path(job.output_path).exists():
        raise HTTPException(404, "成片不存在或尚未渲染完成")
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{job.id}.mp4"'
    return FileResponse(job.output_path, media_type="video/mp4", headers=headers)


@router.get("/jobs/{job_id}/subtitles")
def download_subtitles(job_id: str, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    """下载成片对应的 SRT 字幕文件。"""
    job = _get_job(job_id, tenant, db)
    srt = Path(job.output_path).with_suffix(".srt") if job.output_path else None
    if not srt or not srt.exists():
        raise HTTPException(404, "字幕文件不存在")
    return FileResponse(srt, media_type="text/plain", filename=f"{job.id}.srt")


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    job = _get_job(job_id, tenant, db)
    if job.output_path:
        Path(job.output_path).unlink(missing_ok=True)
        Path(job.output_path).with_suffix(".srt").unlink(missing_ok=True)
    db.delete(job)
    db.commit()
    return {"ok": True}
