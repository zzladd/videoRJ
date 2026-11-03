"""双均线：收盘确认交叉，下一根开盘价成交，费用用该场所的公开吃单费率。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from paper_spot.research.indicators import sma
from paper_spot.research.store import Bar


def dual_ma_grid() -> list[tuple[int, int]]:
    fast_choices = (5, 10, 20)
    slow_choices = (10, 20, 30, 60)
    return [(fast, slow) for fast in fast_choices for slow in slow_choices if fast < slow]


def simulate_dual_ma(
    bars: list[Bar],
    fast: int,
    slow: int,
    taker_rate: Decimal,
    starting_cash: Decimal = Decimal("10000"),
) -> dict[str, Any]:
    if fast < 1 or slow <= fast:
        raise ValueError("慢线必须长于快线")
    if taker_rate <= 0:
        raise ValueError("吃单费率必须是正数")
    prices = [bar.close for bar in bars]
    fast_line = sma([float(price) for price in prices], fast)
    slow_line = sma([float(price) for price in prices], slow)
    cash = starting_cash
    base = Decimal("0")
    entry_cash: Decimal | None = None
    fills: list[dict[str, str | int]] = []
    equity_points: list[Decimal] = []
    peak = starting_cash
    max_drawdown = Decimal("0")
    fees = Decimal("0")
    for index in range(len(bars) - 1):
        equity_points.append(_equity(cash, base, bars[index].close))
        side = _cross(fast_line, slow_line, index)
        if side == "BUY" and base == 0 and cash > 0:
            fee = cash * taker_rate
            net = cash - fee
            price = bars[index + 1].open
            if price <= 0 or net <= 0:
                continue
            base = net / price
            fees += fee
            entry_cash = cash
            cash = Decimal("0")
            fills.append(_fill(bars[index + 1], "BUY", price, base, fee, cash, base, ""))
        elif side == "SELL" and base > 0:
            price = bars[index + 1].open
            gross = base * price
            fee = gross * taker_rate
            cash = gross - fee
            fees += fee
            qty = base
            base = Decimal("0")
            pnl = "" if entry_cash is None else _text(cash - entry_cash)
            entry_cash = None
            fills.append(_fill(bars[index + 1], "SELL", price, qty, fee, cash, base, pnl))
    if bars:
        equity_points.append(_equity(cash, base, bars[-1].close))
    for equity in equity_points:
        if equity > peak:
            peak = equity
        if peak > 0:
            drawdown = (peak - equity) / peak
            if drawdown > max_drawdown:
                max_drawdown = drawdown
    ending = equity_points[-1] if equity_points else starting_cash
    return {
        "fast": fast,
        "slow": slow,
        "starting_cash": str(starting_cash),
        "ending_equity": _text(ending),
        "return_rate": _text((ending - starting_cash) / starting_cash),
        "max_drawdown": _text(max_drawdown),
        "fees": _text(fees),
        "trades": len(fills),
        "fills": fills,
        "signals": _signals(bars, fast_line, slow_line),
    }


def rank_dual_ma(
    bars: list[Bar],
    taker_rate: Decimal,
    starting_cash: Decimal = Decimal("10000"),
) -> list[dict[str, Any]]:
    rows = []
    for fast, slow in dual_ma_grid():
        result = simulate_dual_ma(bars, fast, slow, taker_rate, starting_cash)
        rows.append(
            {
                "fast": fast,
                "slow": slow,
                "return_rate": result["return_rate"],
                "max_drawdown": result["max_drawdown"],
                "fees": result["fees"],
                "trades": result["trades"],
                "ending_equity": result["ending_equity"],
            }
        )
    rows.sort(key=lambda item: Decimal(item["return_rate"]), reverse=True)
    return rows


def _cross(fast_line: list[float | None], slow_line: list[float | None], index: int) -> str | None:
    if index < 1:
        return None
    previous_fast = fast_line[index - 1]
    previous_slow = slow_line[index - 1]
    current_fast = fast_line[index]
    current_slow = slow_line[index]
    if None in (previous_fast, previous_slow, current_fast, current_slow):
        return None
    assert previous_fast is not None and previous_slow is not None
    assert current_fast is not None and current_slow is not None
    if previous_fast <= previous_slow and current_fast > current_slow:
        return "BUY"
    if previous_fast >= previous_slow and current_fast < current_slow:
        return "SELL"
    return None


def _signals(bars: list[Bar], fast_line: list[float | None], slow_line: list[float | None]) -> list[dict[str, str | int]]:
    found = []
    for index in range(len(bars)):
        side = _cross(fast_line, slow_line, index)
        if side is None:
            continue
        found.append(
            {
                "time": bars[index].open_time // 1000,
                "open_time": bars[index].open_time,
                "side": side,
                "price": str(bars[index].close),
            }
        )
    return found


def _fill(
    bar: Bar,
    side: str,
    price: Decimal,
    qty: Decimal,
    fee: Decimal,
    cash: Decimal,
    base: Decimal,
    pnl: str,
) -> dict[str, str | int]:
    return {
        "time": bar.open_time // 1000,
        "open_time": bar.open_time,
        "side": side,
        "price": _text(price),
        "qty": _text(qty),
        "fee": _text(fee),
        "cash": _text(cash),
        "base": _text(base),
        "pnl": pnl,
    }


def _equity(cash: Decimal, base: Decimal, price: Decimal) -> Decimal:
    return cash + base * price


def _text(value: Decimal) -> str:
    return format(value, "f")
