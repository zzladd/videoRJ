"""Celery 应用（SaaS 化时启用：TASK_BACKEND=celery）。

启动 worker:
    celery -A app.tasks.celery_app worker -l info -c 4
"""
from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "video_remix",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,  # 视频任务重，避免囤积
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
)


@celery_app.task(name="app.tasks.analyze_material")
def analyze_material_task(material_id: str) -> None:
    from app.pipeline.analyze import analyze_material

    analyze_material(material_id)


@celery_app.task(name="app.tasks.render_job")
def render_job_task(job_id: str) -> None:
    from app.pipeline.render import run_render_job

    run_render_job(job_id)
