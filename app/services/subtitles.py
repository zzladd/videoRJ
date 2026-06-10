"""字幕生成：根据时间线生成 SRT。"""
from pathlib import Path


def _fmt(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(entries: list[tuple[float, float, str]], path: str | Path) -> Path:
    """entries: [(start, end, text)]，时间为成片时间轴。"""
    lines = []
    idx = 0
    for start, end, text in entries:
        text = text.strip()
        if not text or end <= start:
            continue
        idx += 1
        lines.append(f"{idx}\n{_fmt(start)} --> {_fmt(end)}\n{text}\n")
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return Path(path)
