"""链接下载（基于 yt-dlp，支持抖音等平台）。

合规提示：仅用于下载「已获得合法授权」的素材，授权信息应填入 Material.license_note 留痕。
"""
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp

from app.config import get_settings

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff，。！？、【】（）]+")
_MODAL_ID_RE = re.compile(r"[?&]modal_id=(\d+)")
_DOUYIN_VID_RE = re.compile(r"/(?:video|note)/(\d+)")

_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


class DownloadError(RuntimeError):
    pass


def normalize_url(raw: str) -> str:
    """把用户粘贴的内容规范化为 yt-dlp 可识别的链接。

    支持：
    - 抖音 App 分享文本（如 "8.88 Abc:/ 复制打开抖音 https://v.douyin.com/xxx/ ..."）
    - v.douyin.com 短链（自动展开重定向）
    - 网页版 modal_id 形式（/jingxuan?modal_id=、/discover?modal_id= 等）
      yt-dlp 只识别 douyin.com/video/<id>，需要重写
    """
    m = _URL_RE.search(raw.strip())
    if not m:
        raise DownloadError("未在输入中找到有效链接")
    url = m.group(0).rstrip("/\"'<>)]，。")

    host = urlparse(url).netloc.lower()
    if "douyin.com" not in host:
        return url

    # 短链先展开成真实地址
    if host.startswith(("v.douyin", "v.ixigua")):
        url = _expand_short_url(url)

    # modal_id 形式重写为标准 /video/<id>
    m = _MODAL_ID_RE.search(url)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    # 已是 /video/ 或 /note/ 形式则取纯净链接（去掉多余 query）
    m = _DOUYIN_VID_RE.search(url)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    return url


def _expand_short_url(url: str) -> str:
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": _MOBILE_UA},
            follow_redirects=True,
            timeout=15,
        )
        return str(resp.url)
    except httpx.HTTPError as e:
        logger.warning("短链展开失败，使用原链接: %s (%s)", url, e)
        return url


def download_video(url: str, out_dir: str | Path, material_id: str) -> tuple[Path, str]:
    """下载视频，返回 (文件路径, 标题)。"""
    settings = get_settings()
    url = normalize_url(url)
    logger.info("开始下载: %s", url)

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
        "http_headers": {"Referer": "https://www.douyin.com/"},
    }
    # 抖音部分内容需要登录 Cookie 才能获取播放地址
    if settings.ytdlp_cookies_file and Path(settings.ytdlp_cookies_file).exists():
        opts["cookiefile"] = settings.ytdlp_cookies_file
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
        msg = str(e)
        if "Fresh cookies" in msg or "cookies" in msg.lower():
            msg += "（提示：抖音需要登录 Cookie，请导出浏览器 cookies.txt 并配置 YTDLP_COOKIES_FILE）"
        raise DownloadError(f"下载失败: {msg}") from e
