"""断线窗口和过期行情。撮合不能用这些事件给旧单成交。"""

from __future__ import annotations

import json
from pathlib import Path

from paper_spot.constants import STALE_AFTER_NS


class GapGate:
    def __init__(self, stale_ns: int = STALE_AFTER_NS, path: Path | None = None) -> None:
        self.stale_ns = stale_ns
        self.path = path
        self.gaps: list[tuple[int, int]] = []
        self.dropped = 0

    def add_gap(self, start_ns: int, end_ns: int) -> None:
        if end_ns <= start_ns:
            raise ValueError("断线窗口的结束时间必须晚于开始时间")
        self.gaps.append((start_ns, end_ns))
        self.flush()

    def blocks(self, ts_event: int, ts_init: int) -> bool:
        """行情到达得太晚，或交易所时间落在已记录的断线窗口里。"""
        if ts_init > ts_event and ts_init - ts_event > self.stale_ns:
            return True
        return any(start < ts_event < end for start, end in self.gaps)

    def reject(self, ts_event: int, ts_init: int) -> bool:
        if not self.blocks(ts_event, ts_init):
            return False
        self.dropped += 1
        return True

    def flush(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "stale_ns": self.stale_ns,
            "dropped": self.dropped,
            "gaps": self.gaps,
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.path)
