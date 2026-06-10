"""链接下载（基于 yt-dlp，支持抖音等平台）。

合规提示：仅用于下载「已获得合法授权」的素材，授权信息应填入 Material.license_note 留痕。
"""
import logging
from pathlib import Path

import yt_dlp

logger = logging.getLogger(__name__)


class DownloadError(RuntimeError):
    pass


def download_video(url: str, out_dir: str | Path, material_id: str) -> tuple[Path, str]:
    """下载视频，返回 (文件路径, 标题)。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(out_dir / f"{material_id}_raw.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "format": "bv*+ba/b",  # 最优视频+音频，失败则退化为单文件
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
            # 合并封装后扩展名可能变为 mp4
            if not path.exists():
                candidates = list(out_dir.glob(f"{material_id}_raw.*"))
                if not candidates:
                    raise DownloadError("下载完成但找不到输出文件")
                path = candidates[0]
            title = (info.get("title") or "").strip()
            return path, title
    except Exception as e:  # yt-dlp 异常类型繁杂，统一包装
        raise DownloadError(f"下载失败: {e}") from e
