"""K 线周期。加密现货按交易所的 UTC 边界，不把周末当成缺口。"""

from __future__ import annotations

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}

INTERVAL_LABELS = {
    "1m": "1分钟",
    "5m": "5分钟",
    "15m": "15分钟",
    "30m": "30分钟",
    "1h": "60分钟",
    "1d": "日线",
}


def interval_ms(interval: str) -> int:
    if interval not in INTERVAL_MS:
        raise ValueError(f"不支持的周期：{interval}")
    return INTERVAL_MS[interval]
