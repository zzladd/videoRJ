"""本地 K 线库。已收盘的 K 线按场所、标的、周期落盘，并记录覆盖和缺口。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from paper_spot.research.intervals import interval_ms


@dataclass(frozen=True)
class Bar:
    open_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal = Decimal("0")
    trades: int = 0
    taker_buy_base: Decimal = Decimal("0")


def bars_dir(root: Path, venue: str, symbol: str) -> Path:
    _check_name(venue)
    _check_name(symbol)
    return root / "data" / "bars" / venue / symbol


def parquet_path(root: Path, venue: str, symbol: str, interval: str) -> Path:
    interval_ms(interval)
    return bars_dir(root, venue, symbol) / f"{interval}.parquet"


def coverage_path(root: Path, venue: str, symbol: str, interval: str) -> Path:
    return parquet_path(root, venue, symbol, interval).with_suffix(".coverage.json")


def load_bars(root: Path, venue: str, symbol: str, interval: str) -> list[Bar]:
    path = parquet_path(root, venue, symbol, interval)
    if not path.is_file():
        return []
    table = pq.read_table(path)
    rows = table.to_pylist()
    bars = [
        Bar(
            open_time=int(row["open_time"]),
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            volume=Decimal(row["volume"]),
            quote_volume=Decimal(row["quote_volume"]),
            trades=int(row["trades"]),
            taker_buy_base=Decimal(row["taker_buy_base"]),
        )
        for row in rows
    ]
    bars.sort(key=lambda item: item.open_time)
    return bars


def upsert_bars(root: Path, venue: str, symbol: str, interval: str, incoming: list[Bar]) -> dict:
    existing = {bar.open_time: bar for bar in load_bars(root, venue, symbol, interval)}
    for bar in incoming:
        existing[bar.open_time] = bar
    merged = [existing[key] for key in sorted(existing)]
    path = parquet_path(root, venue, symbol, interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_parquet(path, merged)
    coverage = build_coverage(venue, symbol, interval, merged)
    coverage_path(root, venue, symbol, interval).write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return coverage


def read_coverage(root: Path, venue: str, symbol: str, interval: str) -> dict:
    path = coverage_path(root, venue, symbol, interval)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return build_coverage(venue, symbol, interval, load_bars(root, venue, symbol, interval))


def build_coverage(venue: str, symbol: str, interval: str, bars: list[Bar]) -> dict:
    step = interval_ms(interval)
    if not bars:
        return {
            "venue": venue,
            "symbol": symbol,
            "interval": interval,
            "count": 0,
            "first_open_ms": None,
            "last_open_ms": None,
            "gaps": [],
        }
    gaps: list[list[int]] = []
    for prev, nxt in zip(bars, bars[1:]):
        missing = nxt.open_time - prev.open_time
        if missing > step:
            gaps.append([prev.open_time + step, nxt.open_time])
    return {
        "venue": venue,
        "symbol": symbol,
        "interval": interval,
        "count": len(bars),
        "first_open_ms": bars[0].open_time,
        "last_open_ms": bars[-1].open_time,
        "gaps": gaps,
    }


def slice_bars(bars: list[Bar], start_ms: int | None, end_ms: int | None) -> list[Bar]:
    chosen = bars
    if start_ms is not None:
        chosen = [bar for bar in chosen if bar.open_time >= start_ms]
    if end_ms is not None:
        chosen = [bar for bar in chosen if bar.open_time <= end_ms]
    return chosen


def _write_parquet(path: Path, bars: list[Bar]) -> None:
    table = pa.table(
        {
            "open_time": pa.array([bar.open_time for bar in bars], pa.int64()),
            "open": pa.array([str(bar.open) for bar in bars], pa.string()),
            "high": pa.array([str(bar.high) for bar in bars], pa.string()),
            "low": pa.array([str(bar.low) for bar in bars], pa.string()),
            "close": pa.array([str(bar.close) for bar in bars], pa.string()),
            "volume": pa.array([str(bar.volume) for bar in bars], pa.string()),
            "quote_volume": pa.array([str(bar.quote_volume) for bar in bars], pa.string()),
            "trades": pa.array([bar.trades for bar in bars], pa.int64()),
            "taker_buy_base": pa.array([str(bar.taker_buy_base) for bar in bars], pa.string()),
        }
    )
    temporary = path.with_suffix(".parquet.tmp")
    pq.write_table(table, temporary)
    temporary.replace(path)


def _check_name(value: str) -> None:
    if not value or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for char in value):
        raise ValueError(f"名称只能是大写字母和数字：{value}")
