"""异步任务调度抽象。

- inline 后端：进程内守护线程工作池，零依赖，适合自用单机部署。
  使用守护线程是为了让 Ctrl+C 能立即退出（否则解释器退出时会阻塞等待
  正在执行的渲染/分析任务跑完）；进行中任务的状态由启动时的
  recover_stale_tasks() 兜底标记为失败，可一键重试。
- celery 后端：分布式队列，适合 SaaS 化后水平扩展 worker。

业务代码只调用 dispatch()，切换后端不需要改业务逻辑。
任务状态全部落库（Material.status / RenderJob.status），不依赖队列自身状态。
"""
import logging
import queue
import threading

from app.config import get_settings

logger = logging.getLogger(__name__)

_queue: "queue.Queue[tuple[str, tuple]]" = queue.Queue()
_started = False
_lock = threading.Lock()


def _worker_loop() -> None:
    while True:
        task_name, args = _queue.get()
        try:
            _resolve(task_name)(*args)
        except Exception:  # 兜底日志；任务内部应已把失败状态落库
            logger.exception("任务 %s%s 执行异常", task_name, args)
        finally:
            _queue.task_done()


def _ensure_workers() -> None:
    global _started
    with _lock:
        if _started:
            return
        for i in range(get_settings().task_workers):
            threading.Thread(target=_worker_loop, daemon=True, name=f"task-{i}").start()
        _started = True


def dispatch(task_name: str, *args) -> None:
    """派发任务。task_name: analyze_material | render_job"""
    settings = get_settings()
    if settings.task_backend == "celery":
        from app.tasks.celery_app import celery_app

        celery_app.send_task(f"app.tasks.{task_name}", args=list(args))
        return

    _ensure_workers()
    _queue.put((task_name, args))


def _resolve(task_name: str):
    if task_name == "analyze_material":
        from app.pipeline.analyze import analyze_material

        return analyze_material
    if task_name == "render_job":
        from app.pipeline.render import run_render_job

        return run_render_job
    raise ValueError(f"未知任务: {task_name}")
