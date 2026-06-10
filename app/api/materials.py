"""素材管理 API：上传、链接下载、列表、详情、片段、重新分析、删除。"""
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.auth import get_tenant
from app.config import get_settings
from app.database import get_db
from app.models import Material, Segment, TranscriptLine
from app.pipeline.analyze import delete_material_files
from app.schemas import MaterialFromUrlIn, MaterialOut, SegmentOut
from app.tasks.runner import dispatch

router = APIRouter(prefix="/api/materials", tags=["素材"])

ALLOWED_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".m4v", ".ts"}


def _to_out(mat: Material, db: Session) -> MaterialOut:
    out = MaterialOut.model_validate(mat)
    if mat.cover_path:
        out.cover_url = f"/api/materials/{mat.id}/cover"
    if mat.video_path:
        out.preview_url = f"/api/materials/{mat.id}/preview"
    out.segment_count = db.query(Segment).filter_by(material_id=mat.id).count()
    return out


@router.post("/upload", response_model=MaterialOut)
async def upload_material(
    file: UploadFile,
    title: str = Form(""),
    license_note: str = Form(""),
    tags: str = Form("[]"),
    tenant: str = Depends(get_tenant),
    db: Session = Depends(get_db),
):
    """上传本地视频文件。"""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, f"不支持的文件类型: {ext}")
    try:
        tag_list = json.loads(tags) if tags else []
    except json.JSONDecodeError:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    mat = Material(
        tenant_id=tenant,
        title=title or Path(file.filename or "").stem,
        source_type="upload",
        license_note=license_note,
        tags=tag_list,
    )
    db.add(mat)
    db.flush()

    settings = get_settings()
    dst = settings.materials_dir / f"{mat.id}_raw{ext}"
    with open(dst, "wb") as f:
        shutil.copyfileobj(file.file, f)
    mat.original_path = str(dst)
    db.commit()

    dispatch("analyze_material", mat.id)
    return _to_out(mat, db)


@router.post("/from-url", response_model=MaterialOut)
def create_from_url(
    body: MaterialFromUrlIn,
    tenant: str = Depends(get_tenant),
    db: Session = Depends(get_db),
):
    """通过链接下载素材（抖音等），支持直接粘贴 App 分享文本。仅限已获得合法授权的内容，建议填写 license_note 留痕。"""
    if "http://" not in body.url and "https://" not in body.url:
        raise HTTPException(400, "无效的链接：未找到 http(s) 地址")
    mat = Material(
        tenant_id=tenant,
        title=body.title,
        source_type="url",
        source_url=body.url,
        license_note=body.license_note,
        tags=body.tags,
    )
    db.add(mat)
    db.commit()
    dispatch("analyze_material", mat.id)
    return _to_out(mat, db)


@router.get("", response_model=list[MaterialOut])
def list_materials(
    status: str | None = None,
    tenant: str = Depends(get_tenant),
    db: Session = Depends(get_db),
):
    q = db.query(Material).filter_by(tenant_id=tenant)
    if status:
        q = q.filter_by(status=status)
    return [_to_out(m, db) for m in q.order_by(Material.created_at.desc()).all()]


def _get_material(material_id: str, tenant: str, db: Session) -> Material:
    mat = db.get(Material, material_id)
    if mat is None or mat.tenant_id != tenant:
        raise HTTPException(404, "素材不存在")
    return mat


@router.get("/{material_id}", response_model=MaterialOut)
def get_material(material_id: str, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    return _to_out(_get_material(material_id, tenant, db), db)


@router.get("/{material_id}/segments", response_model=list[SegmentOut])
def list_segments(material_id: str, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    _get_material(material_id, tenant, db)
    return (
        db.query(Segment).filter_by(material_id=material_id).order_by(Segment.start).all()
    )


@router.get("/{material_id}/transcript")
def get_transcript(material_id: str, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    _get_material(material_id, tenant, db)
    lines = (
        db.query(TranscriptLine).filter_by(material_id=material_id).order_by(TranscriptLine.start).all()
    )
    return [{"start": x.start, "end": x.end, "text": x.text} for x in lines]


@router.get("/{material_id}/cover")
def get_cover(material_id: str, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    mat = _get_material(material_id, tenant, db)
    if not mat.cover_path or not Path(mat.cover_path).exists():
        raise HTTPException(404, "封面不存在")
    return FileResponse(mat.cover_path, media_type="image/jpeg")


@router.get("/{material_id}/preview")
def preview_material(material_id: str, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    mat = _get_material(material_id, tenant, db)
    if not mat.video_path or not Path(mat.video_path).exists():
        raise HTTPException(404, "视频文件不存在")
    return FileResponse(mat.video_path, media_type="video/mp4", filename=f"{mat.title or mat.id}.mp4")


@router.post("/{material_id}/reanalyze", response_model=MaterialOut)
def reanalyze(material_id: str, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    mat = _get_material(material_id, tenant, db)
    mat.status = "pending"
    mat.error = ""
    db.commit()
    dispatch("analyze_material", mat.id)
    return _to_out(mat, db)


@router.delete("/{material_id}")
def delete_material(material_id: str, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    mat = _get_material(material_id, tenant, db)
    delete_material_files(mat)
    db.delete(mat)
    db.commit()
    return {"ok": True}
