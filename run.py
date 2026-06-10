"""启动入口（推荐使用，Windows 兼容性最好）：python run.py

相比直接运行 uvicorn 命令的区别：
- Windows 下切换到 Selector 事件循环，规避 Proactor 在客户端断连
  （浏览器拖动/关闭视频预览）时刷 ConnectionResetError 噪音日志的问题
- Ctrl+C 可正常退出（后台任务为守护线程，中断的任务重启后可一键重试）

可用环境变量：HOST（默认 0.0.0.0）、PORT（默认 8000）
"""
import asyncio
import os
import sys


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
