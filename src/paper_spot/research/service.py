"""研究动作：补库、扫描、样本外验证、给图表准备序列。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from paper_spot.config_loader import ConfigError
from paper_spot.config_loader import load_fee
from paper_spot.research.fetch import Opener
from paper_spot.research.fetch import fetch_closed_bars
from paper_spot.research.indicators import atr
from paper_spot.research.indicators import kdj
from paper_spot.research.indicators import macd
from paper_spot.research.indicators import obv
from paper_spot.research.indicators import rsi
from paper_spot.research.indicators import series_points
from paper_spot.research.indicators import sma
from paper_spot.research.intervals import INTERVAL_LABELS
from paper_spot.research.intervals import interval_ms
from paper_spot.research.store import Bar
from paper_spot.research.store import load_bars
from paper_spot.research.store import read_coverage
from paper_spot.research.store import slice_bars
from paper_spot.research.store import upsert_bars
from paper_spot.research.strategy import rank_dual_ma
from paper_spot.research.strategy import simulate_dual_ma

CHART_LIMIT = 5_000
_NOTE = "K 线级验证：交叉在收盘确认，下一根开盘价成交，费用用该场所公开吃单费率。这不是 L2 沙箱成交。实盘通路仍从「发起」进入。"


def parse_utc_date(text: str) -> int:
    try:
        moment = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("日期要写成 YYYY-MM-DD") from exc
    return int(moment.timestamp() * 1000)


def end_of_utc_date(text: str) -> int:
    return parse_utc_date(text) + 86_400_000 - 1


def sync_range(
    root: Path,
    venue: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    opener: Opener | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    now_ms = _now_ms() if now_ms is None else now_ms
    incoming = fetch_closed_bars(venue, symbol, interval, start_ms, end_ms, now_ms, opener)
    if not incoming:
        raise ValueError("这个范围没有拉到已收盘 K 线")
    coverage = upsert_bars(root, venue, symbol, interval, incoming)
    return {"fetched": len(incoming), "coverage": coverage}


def catch_up(
    root: Path,
    venue: str,
    symbol: str,
    interval: str,
    opener: Opener | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """库已存在时只追加最后一根之后的已收盘 K 线。空库不访问交易所。"""
    now_ms = _now_ms() if now_ms is None else now_ms
    bars = load_bars(root, venue, symbol, interval)
    coverage = read_coverage(root, venue, symbol, interval)
    if not bars:
        return {"fetched": 0, "coverage": coverage, "appended": False}
    step = interval_ms(interval)
    start_ms = bars[-1].open_time + step
    last_closed = (now_ms - (now_ms % step)) - step
    if start_ms > last_closed:
        return {"fetched": 0, "coverage": coverage, "appended": False}
    try:
        result = sync_range(root, venue, symbol, interval, start_ms, last_closed, opener, now_ms)
    except ValueError as exc:
        if "没有拉到" in str(exc):
            return {"fetched": 0, "coverage": coverage, "appended": False}
        raise
    result["appended"] = True
    return result


def coverage_report(root: Path, venue: str, symbol: str) -> dict[str, Any]:
    rows = []
    for interval in INTERVAL_LABELS:
        coverage = read_coverage(root, venue, symbol, interval)
        coverage["label"] = INTERVAL_LABELS[interval]
        rows.append(coverage)
    return {"venue": venue, "symbol": symbol, "intervals": rows}


def scan_dual_ma(
    root: Path,
    venue: str,
    symbol: str,
    interval: str,
    start_text: str,
    end_text: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    now_ms = _now_ms() if now_ms is None else now_ms
    bars = _require_window(root, venue, symbol, interval, start_text, end_text, now_ms)
    fee = load_fee(root, venue)
    rows = rank_dual_ma(bars, Decimal(fee["taker_rate"]))
    return {
        "venue": venue,
        "symbol": symbol,
        "interval": interval,
        "start": start_text,
        "end": end_text,
        "count": len(bars),
        "first_open_ms": bars[0].open_time,
        "last_open_ms": bars[-1].open_time,
        "taker_rate": fee["taker_rate"],
        "fee_version": fee["fee_version"],
        "rows": rows,
        "note": _NOTE,
    }


def validate_dual_ma(
    root: Path,
    venue: str,
    symbol: str,
    interval: str,
    fast: int,
    slow: int,
    search_start: str,
    search_end: str,
    valid_start: str,
    valid_end: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    if (search_start, search_end) == (valid_start, valid_end):
        raise ValueError("验证窗口要和扫描窗口不同")
    if parse_utc_date(valid_start) <= end_of_utc_date(search_end):
        raise ValueError("验证窗口必须晚于扫描窗口，并且不能重叠")
    now_ms = _now_ms() if now_ms is None else now_ms
    search_bars = _require_window(root, venue, symbol, interval, search_start, search_end, now_ms)
    valid_bars = _require_window(root, venue, symbol, interval, valid_start, valid_end, now_ms)
    fee = load_fee(root, venue)
    rate = Decimal(fee["taker_rate"])
    search = simulate_dual_ma(search_bars, fast, slow, rate)
    validation = simulate_dual_ma(valid_bars, fast, slow, rate)
    document = {
        "kind": "dual_ma",
        "created_at": datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "venue": venue,
        "symbol": symbol,
        "interval": interval,
        "fast": fast,
        "slow": slow,
        "taker_rate": fee["taker_rate"],
        "fee_version": fee["fee_version"],
        "starting_cash": "10000",
        "search": _window_summary(search_start, search_end, search_bars, search),
        "validation": _window_summary(valid_start, valid_end, valid_bars, validation),
        "note": _NOTE,
    }
    stamp = _experiment_stamp(root, now_ms)
    path = root / "data" / "experiments" / stamp / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    document["id"] = stamp
    return document


def list_experiments(root: Path) -> list[dict[str, Any]]:
    base = root / "data" / "experiments"
    if not base.is_dir():
        return []
    rows = []
    for path in sorted(base.iterdir(), reverse=True):
        result = path / "result.json"
        if not result.is_file():
            continue
        document = json.loads(result.read_text(encoding="utf-8"))
        rows.append(
            {
                "id": path.name,
                "symbol": document.get("symbol"),
                "interval": document.get("interval"),
                "fast": document.get("fast"),
                "slow": document.get("slow"),
                "created_at": document.get("created_at"),
                "validation_return": (document.get("validation") or {}).get("return_rate"),
            }
        )
    return rows[:30]


def load_experiment(root: Path, experiment_id: str) -> dict[str, Any]:
    if len(experiment_id) != 16 or any(char not in "0123456789TZ" for char in experiment_id):
        raise ValueError("实验编号不合法")
    path = root / "data" / "experiments" / experiment_id / "result.json"
    if not path.is_file():
        raise ValueError("没有这个实验")
    return json.loads(path.read_text(encoding="utf-8"))


def chart_payload(
    root: Path,
    venue: str,
    symbol: str,
    interval: str,
    fast: int,
    slow: int,
    start_text: str | None = None,
    end_text: str | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    if fast < 1 or slow <= fast:
        raise ValueError("慢线必须长于快线")
    now_ms = _now_ms() if now_ms is None else now_ms
    bars = load_bars(root, venue, symbol, interval)
    if start_text:
        bars = slice_bars(bars, parse_utc_date(start_text), None)
    if end_text:
        bars = slice_bars(bars, None, end_of_utc_date(end_text))
    truncated = False
    if len(bars) > CHART_LIMIT:
        bars = bars[-CHART_LIMIT:]
        truncated = True
    coverage = read_coverage(root, venue, symbol, interval)
    payload: dict[str, Any] = {
        "venue": venue,
        "symbol": symbol,
        "interval": interval,
        "label": INTERVAL_LABELS[interval],
        "fast": fast,
        "slow": slow,
        "coverage": coverage,
        "count": len(bars),
        "truncated": truncated,
        "candles": [],
        "volume": [],
        "volume_ma": [],
        "sma_fast": [],
        "sma_slow": [],
        "macd": {"dif": [], "dea": [], "hist": []},
        "rsi": [],
        "kdj": {"k": [], "d": [], "j": []},
        "atr": [],
        "obv": [],
        "signals": [],
        "fills": [],
        "summary": None,
        "note": _NOTE,
    }
    if not bars:
        return payload
    closes = [float(bar.close) for bar in bars]
    volumes = [float(bar.volume) for bar in bars]
    payload["candles"] = [
        {
            "time": bar.open_time // 1000,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
        }
        for bar in bars
    ]
    payload["volume"] = [
        {"time": bar.open_time // 1000, "value": float(bar.volume), "up": bar.close >= bar.open}
        for bar in bars
    ]
    payload["volume_ma"] = series_points(bars, sma(volumes, 20))
    payload["sma_fast"] = series_points(bars, sma(closes, fast))
    payload["sma_slow"] = series_points(bars, sma(closes, slow))
    macd_lines = macd(closes)
    payload["macd"] = {
        "dif": series_points(bars, macd_lines["dif"]),
        "dea": series_points(bars, macd_lines["dea"]),
        "hist": _hist_points(bars, macd_lines["hist"]),
    }
    payload["rsi"] = series_points(bars, rsi(closes))
    kdj_lines = kdj(bars)
    payload["kdj"] = {name: series_points(bars, values) for name, values in kdj_lines.items()}
    payload["atr"] = series_points(bars, atr(bars))
    payload["obv"] = series_points(bars, obv(bars))
    try:
        fee = load_fee(root, venue)
        simulated = simulate_dual_ma(bars, fast, slow, Decimal(fee["taker_rate"]))
    except (ConfigError, KeyError, ValueError, OSError):
        simulated = None
        fee = None
    if simulated is not None:
        payload["signals"] = simulated["signals"]
        payload["fills"] = simulated["fills"]
        payload["summary"] = {
            "return_rate": simulated["return_rate"],
            "max_drawdown": simulated["max_drawdown"],
            "fees": simulated["fees"],
            "trades": simulated["trades"],
            "ending_equity": simulated["ending_equity"],
            "taker_rate": str(fee["taker_rate"]) if fee else "",
        }
    payload["gaps"] = _missing_in_window(
        bars,
        bars[0].open_time,
        bars[-1].open_time,
        interval,
        now_ms,
    )
    return payload


def _require_window(
    root: Path,
    venue: str,
    symbol: str,
    interval: str,
    start_text: str,
    end_text: str,
    now_ms: int,
) -> list[Bar]:
    start_ms = parse_utc_date(start_text)
    end_ms = end_of_utc_date(end_text)
    if end_ms < start_ms:
        raise ValueError("结束日期早于开始日期")
    step = interval_ms(interval)
    if (end_ms - start_ms) // step + 1 > 50_000:
        raise ValueError("一次最多检查 50000 根，请缩小窗口")
    bars = slice_bars(load_bars(root, venue, symbol, interval), start_ms, end_ms)
    gaps = _missing_in_window(bars, start_ms, end_ms, interval, now_ms)
    if gaps:
        raise ValueError("这个窗口有缺口，先补齐本地 K 线再扫描")
    if not bars:
        raise ValueError("这个窗口没有已收盘的本地 K 线")
    return bars


def _missing_in_window(
    bars: list[Bar],
    start_ms: int,
    end_ms: int,
    interval: str,
    now_ms: int,
) -> list[list[int]]:
    step = interval_ms(interval)
    first = _align_up(start_ms, step)
    last_closed = (now_ms - (now_ms % step)) - step
    last = min(_align_down(end_ms, step), last_closed)
    if first > last:
        return [[start_ms, end_ms]]
    expected = (last - first) // step + 1
    if expected > 50_000:
        return [[first, last + step]]
    have = {bar.open_time for bar in bars}
    gaps: list[list[int]] = []
    gap_start: int | None = None
    cursor = first
    while cursor <= last:
        if cursor not in have:
            if gap_start is None:
                gap_start = cursor
        elif gap_start is not None:
            gaps.append([gap_start, cursor])
            gap_start = None
        cursor += step
    if gap_start is not None:
        gaps.append([gap_start, last + step])
    return gaps


def _window_summary(start: str, end: str, bars: list[Bar], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "first_open_ms": bars[0].open_time,
        "last_open_ms": bars[-1].open_time,
        "count": len(bars),
        "return_rate": result["return_rate"],
        "max_drawdown": result["max_drawdown"],
        "fees": result["fees"],
        "trades": result["trades"],
        "ending_equity": result["ending_equity"],
        "fills": result["fills"],
        "signals": result["signals"],
    }


def _hist_points(bars: list[Bar], values: list[float | None]) -> list[dict[str, float | bool]]:
    points = []
    for bar, value in zip(bars, values):
        if value is None:
            continue
        points.append({"time": bar.open_time // 1000, "value": value, "up": value >= 0})
    return points


def _experiment_stamp(root: Path, now_ms: int) -> str:
    for index in range(100):
        candidate = datetime.fromtimestamp((now_ms + index * 1000) / 1000, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if not (root / "data" / "experiments" / candidate).exists():
            return candidate
    raise ValueError("同一秒里的实验太多")


def _align_up(value: int, step: int) -> int:
    if value % step == 0:
        return value
    return value + (step - value % step)


def _align_down(value: int, step: int) -> int:
    return value - (value % step)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
