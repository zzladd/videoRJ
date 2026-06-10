"""异步任务调度抽象。

- inline 后端：进程内线程池，零依赖，适合自用单机部署。
- celery 后端：分布式队列，适合 SaaS 化后水平扩展 worker。

业务代码只调用 dispatch()，切换后端不需要改业务逻辑。
任务状态全部落库（Material.status / RenderJob.status），不依赖队列自身状态。
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from app.config import get_settings

logger = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=get_settings().task_workers, thread_name_prefix="task"
            )
        return _executor


def dispatch(task_name: str, *args) -> None:
    """派发任务。task_name: analyze_material | render_job"""
    settings = get_settings()
    if settings.task_backend == "celery":
        from app.tasks.celery_app import celery_app

        celery_app.send_task(f"app.tasks.{task_name}", args=list(args))
        return

    fn = _resolve(task_name)

    def _run() -> None:
        try:
            fn(*args)
        except Exception:  # 兜底日志；任务内部应已把失败状态落库
            logger.exception("任务 %s%s 执行异常", task_name, args)

    _get_executor().submit(_run)


def _resolve(task_name: str):
    if task_name == "analyze_material":
        from app.pipeline.analyze import analyze_material

        return analyze_material
    if task_name == "render_job":
        from app.pipeline.render import run_render_job

        return run_render_job
    raise ValueError(f"未知任务: {task_name}")
