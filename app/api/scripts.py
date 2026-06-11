"""脚本 API：AI 生成内容脚本 -> 转执行脚本 JSON -> 人工编辑。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth import get_tenant
from app.database import get_db
from app.models import Script
from app.schemas import ExecutionScript, ScriptGenerateIn, ScriptOut, ScriptUpdateIn
from app.services.script_gen import generate_content_script, generate_execution_script

router = APIRouter(prefix="/api/scripts", tags=["脚本"])


@router.post("/generate", response_model=ScriptOut)
def generate(body: ScriptGenerateIn, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    """AI 一键生成：内容脚本 + 结构化执行脚本。"""
    content = generate_content_script(body.topic, body.style, body.target_duration, body.extra_requirements)
    execution = generate_execution_script(content, body.topic, body.target_duration)
    script = Script(
        tenant_id=tenant,
        title=execution.title or body.topic[:50],
        topic=body.topic,
        content=content,
        execution=execution.model_dump(),
        status="ready",
    )
    db.add(script)
    db.commit()
    return script


def _get_script(script_id: str, tenant: str, db: Session) -> Script:
    script = db.get(Script, script_id)
    if script is None or script.tenant_id != tenant:
        raise HTTPException(404, "脚本不存在")
    return script


@router.post("/{script_id}/to-execution", response_model=ScriptOut)
def regenerate_execution(script_id: str, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    """根据（可能已人工修改的）内容脚本重新生成执行脚本 JSON。"""
    script = _get_script(script_id, tenant, db)
    if not script.content.strip():
        raise HTTPException(400, "内容脚本为空")
    execution = generate_execution_script(script.content, script.topic, 30.0)
    script.execution = execution.model_dump()
    script.status = "ready"
    db.commit()
    return script


@router.get("", response_model=list[ScriptOut])
def list_scripts(tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    return (
        db.query(Script).filter_by(tenant_id=tenant).order_by(Script.created_at.desc()).all()
    )


@router.get("/{script_id}", response_model=ScriptOut)
def get_script(script_id: str, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    return _get_script(script_id, tenant, db)


@router.put("/{script_id}", response_model=ScriptOut)
def update_script(
    script_id: str,
    body: ScriptUpdateIn,
    tenant: str = Depends(get_tenant),
    db: Session = Depends(get_db),
):
    """人工编辑内容脚本或执行脚本（执行脚本会做 Schema 校验）。"""
    script = _get_script(script_id, tenant, db)
    if body.title is not None:
        script.title = body.title
    if body.content is not None:
        script.content = body.content
    if body.execution is not None:
        try:
            validated = ExecutionScript.model_validate(body.execution)
        except ValidationError as e:
            raise HTTPException(422, f"执行脚本不符合 Schema: {e}") from e
        script.execution = validated.model_dump()
        script.status = "ready"
    db.commit()
    return script


@router.delete("/{script_id}")
def delete_script(script_id: str, tenant: str = Depends(get_tenant), db: Session = Depends(get_db)):
    script = _get_script(script_id, tenant, db)
    db.delete(script)
    db.commit()
    return {"ok": True}
