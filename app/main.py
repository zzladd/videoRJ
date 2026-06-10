"""应用入口。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
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

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    init_db()
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
