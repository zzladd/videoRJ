"""自动混剪合成：按时间线裁剪片段 -> 统一规格 -> 转场拼接 -> 字幕 -> 成片。

转场实现：全部硬切时走 concat demuxer 快路径；含转场时构建
xfade（视频）+ acrossfade/concat（音频）滤镜链，画面与音频同步过渡。
"""
import logging
import shutil
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

from app.config import get_settings
from app.services.matcher import TimelineClip
from app.services.media import MediaError, missing_binary_msg
from app.services.subtitles import allocate_cues, write_srt

logger = logging.getLogger(__name__)

# ShotSpec.transition -> ffmpeg xfade transition 名称
_XFADE_MAP = {
    "dissolve": "fade",       # 经典叠化，切换最柔和
    "fade": "fadeblack",      # 经黑场过渡
    "slide": "slideleft",
    "wipe": "wipeleft",
}


def _run(cmd: list[str], timeout: int = 3600) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise MediaError(missing_binary_msg(cmd[0])) from e
    if proc.returncode != 0:
        raise MediaError(f"合成命令失败: {' '.join(cmd[:8])}...\n{proc.stderr[-2000:]}")


def compose(
    timeline: list[TimelineClip],
    material_paths: dict[str, str],
    output_path: str | Path,
    *,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    keep_source_audio: bool = False,
    bgm_path: str | None = None,
    subtitle_mode: str = "burn",
    subtitle_font_size: int = 16,
    on_progress: Callable[[float, str], None] | None = None,
) -> Path:
    """渲染成片。material_paths: {material_id: 标准化视频路径}"""
    settings = get_settings()
    output_path = Path(output_path)
    work_dir = settings.tmp_dir / f"compose_{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=True)

    def report(p: float, msg: str) -> None:
        if on_progress:
            on_progress(p, msg)

    try:
        # 1. 逐片段裁剪并统一规格
        clip_files: list[Path] = []
        clip_durs: list[float] = []
        for i, clip in enumerate(timeline):
            src = material_paths.get(clip.material_id)
            if not src or not Path(src).exists():
                raise MediaError(f"素材文件缺失: {clip.material_id}")
            dst = work_dir / f"clip_{i:03d}.mp4"
            clip_dur = max(0.2, clip.end - clip.start)
            _cut_and_normalize(
                src, dst, clip.start, clip_dur,
                width=width, height=height, fps=fps,
                keep_audio=keep_source_audio,
                # 首镜头 fade = 从黑场淡入；后续镜头的转场在拼接阶段处理
                fade_in=(i == 0 and clip.transition == "fade"),
            )
            clip_files.append(dst)
            clip_durs.append(clip_dur)
            report(0.1 + 0.55 * (i + 1) / len(timeline), f"已合成片段 {i + 1}/{len(timeline)}")

        # 2. 拼接（计算每个镜头在成片时间轴上的起点，供字幕精确对齐）
        merged = work_dir / "merged.mp4"
        starts = _assemble(clip_files, clip_durs, timeline, merged, work_dir)
        report(0.75, "片段拼接完成")

        # 字幕：按镜头实际起点对齐，长文案拆分为多条与画面同步
        srt_entries: list[tuple[float, float, str]] = []
        for i, clip in enumerate(timeline):
            if clip.narration:
                srt_entries.extend(allocate_cues(clip.narration, starts[i], clip_durs[i]))

        # 3. BGM 混音（可选）
        if bgm_path and Path(bgm_path).exists():
            with_bgm = work_dir / "with_bgm.mp4"
            _mix_bgm(merged, bgm_path, with_bgm, duck=keep_source_audio)
            merged = with_bgm
            report(0.82, "背景音乐已混入")

        # 4. 字幕
        srt_path: Path | None = None
        if srt_entries and subtitle_mode != "none":
            srt_path = write_srt(srt_entries, work_dir / "subtitles.srt")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        if srt_path and subtitle_mode == "burn":
            style = (
                f"FontSize={subtitle_font_size},Outline=1,Shadow=0,"
                "PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,MarginV=40"
            )
            _run([
                settings.ffmpeg_bin, "-y", "-i", str(merged),
                "-vf", f"subtitles={_escape_filter_path(srt_path)}:force_style='{style}'",
                "-c:v", "libx264", "-preset", "fast", "-crf", "21",
                "-c:a", "copy", "-movflags", "+faststart",
                str(output_path),
            ])
        elif srt_path and subtitle_mode == "soft":
            _run([
                settings.ffmpeg_bin, "-y", "-i", str(merged), "-i", str(srt_path),
                "-map", "0", "-map", "1",
                "-c", "copy", "-c:s", "mov_text",
                "-metadata:s:s:0", "language=chi",
                "-movflags", "+faststart",
                str(output_path),
            ])
        else:
            shutil.move(str(merged), str(output_path))
        # 字幕文件随成片保留一份，便于二次编辑
        if srt_path:
            shutil.copy(str(srt_path), str(output_path.with_suffix(".srt")))

        report(1.0, "渲染完成")
        return output_path
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _assemble(
    clip_files: list[Path],
    clip_durs: list[float],
    timeline: list[TimelineClip],
    merged: Path,
    work_dir: Path,
) -> list[float]:
    """拼接所有片段，返回每个镜头在成片时间轴上的起点。

    全为硬切时走 concat demuxer（流复制，零损耗）；
    含转场时构建 xfade + acrossfade 滤镜链（重叠过渡，总时长缩短转场时间之和）。
    """
    settings = get_settings()
    n = len(clip_files)
    has_transition = any(c.transition in _XFADE_MAP for c in timeline[1:])

    if n == 1 or not has_transition:
        starts = [0.0]
        for d in clip_durs[:-1]:
            starts.append(starts[-1] + d)
        concat_list = work_dir / "concat.txt"
        # concat demuxer 会把相对路径解析为「相对列表文件所在目录」，必须写绝对路径
        concat_list.write_text(
            "\n".join(f"file '{p.resolve().as_posix()}'" for p in clip_files), encoding="utf-8"
        )
        _run([
            settings.ffmpeg_bin, "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list), "-c", "copy", str(merged),
        ])
        return starts

    # ---- xfade 滤镜链 ----
    lines: list[str] = []
    for i in range(n):
        lines.append(f"[{i}:v]format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS[v{i}]")
        lines.append(f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]")

    vacc, aacc = "v0", "a0"
    total = clip_durs[0]
    starts = [0.0]
    for k in range(1, n):
        clip = timeline[k]
        xf = _XFADE_MAP.get(clip.transition)
        if xf is None:  # 硬切
            lines.append(f"[{vacc}][v{k}]concat=n=2:v=1:a=0[vc{k}]")
            lines.append(f"[{aacc}][a{k}]concat=n=2:v=0:a=1[ac{k}]")
            starts.append(total)
            total += clip_durs[k]
        else:
            # 转场时长不能超过相邻两个片段各自的一半，否则 xfade/acrossfade 会出错
            d = max(0.1, min(clip.transition_duration, clip_durs[k - 1] / 2, clip_durs[k] / 2))
            offset = max(0.0, total - d)
            lines.append(
                f"[{vacc}][v{k}]xfade=transition={xf}:duration={d:.3f}:offset={offset:.3f}[vc{k}]"
            )
            lines.append(f"[{aacc}][a{k}]acrossfade=d={d:.3f}[ac{k}]")
            starts.append(offset)
            total = offset + clip_durs[k]
        vacc, aacc = f"vc{k}", f"ac{k}"

    cmd = [settings.ffmpeg_bin, "-y"]
    for p in clip_files:
        cmd += ["-i", str(p)]
    cmd += [
        "-filter_complex", ";".join(lines),
        "-map", f"[{vacc}]", "-map", f"[{aacc}]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "21",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(merged),
    ]
    _run(cmd)
    return starts


def _cut_and_normalize(
    src: str | Path, dst: Path, start: float, duration: float,
    *, width: int, height: int, fps: int, keep_audio: bool, fade_in: bool,
) -> None:
    """裁剪片段并统一为目标分辨率/帧率；无声或静音时补无声轨，保证拼接一致。"""
    vf_parts = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black",
        f"fps={fps}",
        "setsar=1",
    ]
    if fade_in:
        fade_d = min(0.3, duration / 4)
        vf_parts.append(f"fade=t=in:st=0:d={fade_d}")

    cmd = _build_cut_cmd(src, start, duration, vf_parts, keep_audio=keep_audio)
    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "21",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-shortest", str(dst),
    ]
    _run(cmd)


def _has_audio(path: str | Path) -> bool:
    settings = get_settings()
    try:
        proc = subprocess.run(
            [settings.ffprobe_bin, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
        )
    except FileNotFoundError as e:
        raise MediaError(missing_binary_msg(settings.ffprobe_bin)) from e
    return "audio" in proc.stdout


def _build_cut_cmd(
    src: str | Path, start: float, duration: float, vf_parts: list[str], *, keep_audio: bool,
) -> list[str]:
    settings = get_settings()
    vf = ",".join(vf_parts)
    base = [settings.ffmpeg_bin, "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src)]
    if keep_audio and _has_audio(src):
        return base + ["-vf", vf, "-map", "0:v:0", "-map", "0:a:0"]
    # 静音或源无音轨：补一条无声轨
    return base + [
        "-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=44100:cl=stereo",
        "-vf", vf, "-map", "0:v:0", "-map", "1:a:0",
    ]


def _mix_bgm(video: Path, bgm: str | Path, dst: Path, duck: bool) -> None:
    """混入 BGM；duck=True 时压低 BGM 保留原声，否则 BGM 作为唯一音轨。"""
    settings = get_settings()
    if duck:
        fc = "[1:a]volume=0.25,aloop=loop=-1:size=2e9[bgm];[0:a][bgm]amix=inputs=2:duration=first[a]"
    else:
        fc = "[1:a]volume=0.8,aloop=loop=-1:size=2e9[a]"
    _run([
        settings.ffmpeg_bin, "-y", "-i", str(video), "-stream_loop", "-1", "-i", str(bgm),
        "-filter_complex", fc,
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        "-shortest", str(dst),
    ])


def _escape_filter_path(path: Path) -> str:
    # ffmpeg filter 参数中的路径需转义冒号等字符
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
