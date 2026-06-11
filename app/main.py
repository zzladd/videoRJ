"""应用入口。

启动（推荐，Windows 兼容性最好）：python run.py
或：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import jobs, materials, scripts
from app.config import get_settings
from app.database import init_db
from app.services.media import check_tools, missing_binary_msg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


class _SuppressClientDisconnect(logging.Filter):
    """过滤客户端正常断连产生的噪音日志。

    浏览器播放/拖动视频预览时会主动断开连接，Windows Proactor 事件循环
    会把这种断连当异常打出来（ConnectionResetError / WinError 10054），
    属于正常现象而非故障。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return False
        msg = record.getMessage()
        return "ConnectionResetError" not in msg and "WinError 10054" not in msg


logging.getLogger("asyncio").addFilter(_SuppressClientDisconnect())

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def recover_stale_tasks() -> None:
    """服务重启后，把上次中断的任务标记为失败，前端可一键重试。"""
    from app.database import SessionLocal
    from app.models import Material, RenderJob

    db = SessionLocal()
    try:
        stale_jobs = (
            db.query(RenderJob)
            .filter(RenderJob.status.in_(("queued", "matching", "rendering")))
            .all()
        )
        for job in stale_jobs:
            job.status = "failed"
            job.message = "服务重启导致任务中断，请点击重试"
        stale_mats = (
            db.query(Material)
            .filter(Material.status.in_(("pending", "downloading", "analyzing")))
            .all()
        )
        for mat in stale_mats:
            mat.status = "failed"
            mat.error = "服务重启导致分析中断，请点击重试"
        if stale_jobs or stale_mats:
            db.commit()
            logging.getLogger("app").info(
                "已恢复中断任务: %d 个渲染任务、%d 个素材标记为失败待重试",
                len(stale_jobs), len(stale_mats),
            )
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    init_db()
    recover_stale_tasks()
    tools = check_tools()
    for name, ok in tools.items():
        if not ok:
            bin_path = settings.ffmpeg_bin if name == "ffmpeg" else settings.ffprobe_bin
            logging.getLogger("app").warning(
                "依赖检测失败: %s 不可用！素材分析与渲染将无法工作。%s",
                name, missing_binary_msg(bin_path),
            )
    yield


app = FastAPI(
    title="AI 视频混剪系统",
    description="素材管理 → AI 脚本 → 自动匹配 → 混剪渲染。自用与 SaaS 共用同一套 API。",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(materials.router)
app.include_router(scripts.router)
app.include_router(jobs.router)


@app.get("/api/health")
def health():
    tools = check_tools()
    return {"status": "ok" if all(tools.values()) else "degraded", "tools": tools}


# Web 管理界面
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")
