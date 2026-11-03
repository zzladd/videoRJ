"""用本地已收盘 K 线计算指标。同花顺的 MACD 柱是 2×(DIF−DEA)。"""

from __future__ import annotations

from decimal import Decimal

from paper_spot.research.store import Bar


def closes(bars: list[Bar]) -> list[float]:
    return [float(bar.close) for bar in bars]


def sma(values: list[float], period: int) -> list[float | None]:
    if period < 1:
        raise ValueError("周期至少是 1")
    out: list[float | None] = []
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= period:
            total -= values[index - period]
        if index + 1 < period:
            out.append(None)
        else:
            out.append(total / period)
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    if period < 1:
        raise ValueError("周期至少是 1")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1)
    previous = seed
    for index in range(period, len(values)):
        previous = values[index] * alpha + previous * (1 - alpha)
        out[index] = previous
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, list[float | None]]:
    fast_line = ema(values, fast)
    slow_line = ema(values, slow)
    dif: list[float | None] = []
    for left, right in zip(fast_line, slow_line):
        if left is None or right is None:
            dif.append(None)
        else:
            dif.append(left - right)
    present = [value for value in dif if value is not None]
    dea_present = ema(present, signal)
    dea: list[float | None] = []
    cursor = 0
    for value in dif:
        if value is None:
            dea.append(None)
            continue
        dea.append(dea_present[cursor])
        cursor += 1
    hist: list[float | None] = []
    for left, right in zip(dif, dea):
        if left is None or right is None:
            hist.append(None)
        else:
            hist.append(2 * (left - right))
    return {"dif": dif, "dea": dea, "hist": hist}


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[index] = _rsi_value(avg_gain, avg_loss)
    return out


def kdj(bars: list[Bar], period: int = 9, k_period: int = 3, d_period: int = 3) -> dict[str, list[float | None]]:
    rsv: list[float | None] = []
    for index, bar in enumerate(bars):
        window = bars[max(0, index - period + 1) : index + 1]
        if len(window) < period:
            rsv.append(None)
            continue
        highest = max(float(item.high) for item in window)
        lowest = min(float(item.low) for item in window)
        if highest == lowest:
            rsv.append(50.0)
        else:
            rsv.append((float(bar.close) - lowest) / (highest - lowest) * 100)
    k_line: list[float | None] = [None] * len(bars)
    d_line: list[float | None] = [None] * len(bars)
    j_line: list[float | None] = [None] * len(bars)
    k_value = 50.0
    d_value = 50.0
    for index, value in enumerate(rsv):
        if value is None:
            continue
        k_value = (k_value * (k_period - 1) + value) / k_period
        d_value = (d_value * (d_period - 1) + k_value) / d_period
        k_line[index] = k_value
        d_line[index] = d_value
        j_line[index] = 3 * k_value - 2 * d_value
    return {"k": k_line, "d": d_line, "j": j_line}


def atr(bars: list[Bar], period: int = 14) -> list[float | None]:
    true_ranges: list[float] = []
    for index, bar in enumerate(bars):
        high = float(bar.high)
        low = float(bar.low)
        if index == 0:
            true_ranges.append(high - low)
            continue
        previous = float(bars[index - 1].close)
        true_ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
    smoothed = _wilder(true_ranges, period)
    return smoothed


def obv(bars: list[Bar]) -> list[float | None]:
    total = 0.0
    out: list[float | None] = []
    previous: float | None = None
    for bar in bars:
        close = float(bar.close)
        volume = float(bar.volume)
        if previous is not None:
            if close > previous:
                total += volume
            elif close < previous:
                total -= volume
        out.append(total)
        previous = close
    return out


def series_points(bars: list[Bar], values: list[float | None]) -> list[dict[str, float]]:
    points = []
    for bar, value in zip(bars, values):
        if value is None:
            continue
        points.append({"time": bar.open_time // 1000, "value": value})
    return points


def decimal_close(value: float) -> Decimal:
    return Decimal(str(value))


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    relative = avg_gain / avg_loss
    return 100 - 100 / (1 + relative)


def _wilder(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    previous = seed
    for index in range(period, len(values)):
        previous = (previous * (period - 1) + values[index]) / period
        out[index] = previous
    return out
