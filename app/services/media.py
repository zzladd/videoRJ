"""ffmpeg / ffprobe 封装：探测、转码、抽帧、封面、场景切分、音频提取。"""
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


class MediaError(RuntimeError):
    pass


def missing_binary_msg(bin_name: str) -> str:
    return (
        f"未找到可执行文件「{bin_name}」。请先安装 ffmpeg 并加入系统 PATH，"
        "或在 .env 中将 FFMPEG_BIN / FFPROBE_BIN 配置为完整路径。"
        "Windows 安装方式：winget install Gyan.FFmpeg（装完重开终端），"
        "或从 https://www.gyan.dev/ffmpeg/builds/ 下载解压后配置路径，"
        "例如 FFMPEG_BIN=D:\\ffmpeg\\bin\\ffmpeg.exe"
    )


def check_tools() -> dict[str, bool]:
    """检测 ffmpeg / ffprobe 是否可用（供启动检查与健康检查使用）。"""
    import shutil

    s = get_settings()
    return {
        "ffmpeg": shutil.which(s.ffmpeg_bin) is not None,
        "ffprobe": shutil.which(s.ffprobe_bin) is not None,
    }


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    logger.debug("exec: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise MediaError(missing_binary_msg(cmd[0])) from e
    if proc.returncode != 0:
        raise MediaError(f"命令失败: {' '.join(cmd[:6])}...\n{proc.stderr[-2000:]}")
    return proc


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def probe(path: str | Path) -> MediaInfo:
    s = get_settings()
    proc = _run([
        s.ffprobe_bin, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    data = json.loads(proc.stdout)
    duration = float(data.get("format", {}).get("duration") or 0)
    width = height = 0
    fps = 0.0
    has_audio = False
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and not width:
            width = int(st.get("width") or 0)
            height = int(st.get("height") or 0)
            rate = st.get("avg_frame_rate") or "0/1"
            num, _, den = rate.partition("/")
            try:
                fps = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                fps = 0.0
        if st.get("codec_type") == "audio":
            has_audio = True
    if not width:
        raise MediaError(f"未检测到视频流: {path}")
    return MediaInfo(duration=duration, width=width, height=height, fps=fps, has_audio=has_audio)


def transcode_standard(src: str | Path, dst: str | Path) -> None:
    """统一转码为 H.264 + AAC mp4（faststart），限制最长边，保证后续处理一致性。"""
    s = get_settings()
    edge = s.transcode_max_edge
    # 最长边超限则等比缩小，并保证宽高为偶数
    vf = (
        f"scale='if(gt(iw,ih),min(iw,{edge}),-2)':'if(gt(iw,ih),-2,min(ih,{edge}))',"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )
    _run([
        s.ffmpeg_bin, "-y", "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(dst),
    ])


def extract_cover(src: str | Path, dst: str | Path, at: float = 1.0) -> None:
    s = get_settings()
    _run([
        s.ffmpeg_bin, "-y", "-ss", str(at), "-i", str(src),
        "-frames:v", "1", "-q:v", "3", str(dst),
    ])


def extract_frames(src: str | Path, out_dir: str | Path, interval: float | None = None) -> list[tuple[float, Path]]:
    """按固定间隔抽帧，返回 [(时间戳, 帧路径)]。"""
    s = get_settings()
    interval = interval or s.frame_interval
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _run([
        s.ffmpeg_bin, "-y", "-i", str(src),
        "-vf", f"fps=1/{interval}",
        "-q:v", "4",
        str(out_dir / "frame_%05d.jpg"),
    ])
    frames = sorted(out_dir.glob("frame_*.jpg"))
    return [(i * interval, p) for i, p in enumerate(frames)]


_SCENE_PTS_RE = re.compile(r"pts_time:(\d+(?:\.\d+)?)")


def detect_scenes(src: str | Path, duration: float, min_len: float = 1.0) -> list[tuple[float, float]]:
    """基于 ffmpeg scene score 的场景切分，返回 [(start, end)]；过短的场景会向后合并。"""
    s = get_settings()
    try:
        proc = subprocess.run(
            [
                s.ffmpeg_bin, "-i", str(src),
                "-vf", f"select='gt(scene,{s.scene_threshold})',showinfo",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=3600,
        )
    except FileNotFoundError as e:
        raise MediaError(missing_binary_msg(s.ffmpeg_bin)) from e
    cuts = [float(m.group(1)) for m in _SCENE_PTS_RE.finditer(proc.stderr)]
    cuts = sorted(t for t in set(cuts) if 0 < t < duration)

    bounds = [0.0, *cuts, duration]
    scenes: list[tuple[float, float]] = []
    seg_start = bounds[0]
    for i in range(1, len(bounds)):
        seg_end = bounds[i]
        if seg_end - seg_start >= min_len or i == len(bounds) - 1:
            scenes.append((seg_start, seg_end))
            seg_start = seg_end
    if not scenes:
        scenes = [(0.0, duration)]
    # 末段过短则并入前一段
    if len(scenes) > 1 and scenes[-1][1] - scenes[-1][0] < min_len:
        last = scenes.pop()
        prev = scenes.pop()
        scenes.append((prev[0], last[1]))
    return scenes


def extract_audio(src: str | Path, dst: str | Path) -> None:
    """提取 16k 单声道 wav，供 ASR 使用。"""
    s = get_settings()
    _run([
        s.ffmpeg_bin, "-y", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dst),
    ])
