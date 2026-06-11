"""字幕生成：根据时间线生成 SRT，长文案自动拆分与画面对齐。"""
import re
from pathlib import Path

_SPLIT_RE = re.compile(r"[。！？!?；;]|[，,、]\s*")


def split_narration(text: str, max_len: int = 18) -> list[str]:
    """把长文案按标点拆成适合单屏显示的短句（每条约不超过 max_len 字）。"""
    text = text.strip()
    if not text:
        return []
    # 先按句末标点切，再对过长的子句按逗号/长度二次切
    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if _SPLIT_RE.fullmatch(ch) or len(buf.rstrip("，,、")) >= max_len:
            cleaned = buf.strip().strip("，,、")
            if cleaned:
                parts.append(cleaned)
            buf = ""
    cleaned = buf.strip().strip("，,、")
    if cleaned:
        parts.append(cleaned)
    return parts or [text]


def allocate_cues(text: str, start: float, duration: float, max_len: int = 18) -> list[tuple[float, float, str]]:
    """把一个镜头的文案拆分为多条字幕，按字数比例分配显示时间。"""
    chunks = split_narration(text, max_len)
    if not chunks:
        return []
    total_chars = sum(len(c) for c in chunks)
    cues: list[tuple[float, float, str]] = []
    cursor = start
    for c in chunks:
        span = duration * len(c) / total_chars
        cues.append((cursor, cursor + span, c))
        cursor += span
    return cues


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
