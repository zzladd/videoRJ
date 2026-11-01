"""成交簿、数据质量和日收益摘要。"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from paper_spot.constants import CLOCK_TOLERANCE_NS
from paper_spot.constants import GAP_THRESHOLD_NS


def _tag_map(tags: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for tag in tags:
        if "=" in tag:
            key, value = tag.split("=", 1)
            found[key] = value
    return found


def quote_frame(quotes: list) -> pd.DataFrame:
    rows = [
        {
            "ts_event": quote.ts_event,
            "ts_init": quote.ts_init,
            "bid": Decimal(str(quote.bid_price)),
            "ask": Decimal(str(quote.ask_price)),
            "bid_size": Decimal(str(quote.bid_size)),
            "ask_size": Decimal(str(quote.ask_size)),
        }
        for quote in quotes
    ]
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values("ts_init")
    return frame


def trade_frame(trades: list) -> pd.DataFrame:
    rows = [
        {
            "ts_event": trade.ts_event,
            "ts_init": trade.ts_init,
            "price": Decimal(str(trade.price)),
            "size": Decimal(str(trade.size)),
        }
        for trade in trades
    ]
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values("ts_init")
    return frame


def data_quality(
    quotes: pd.DataFrame,
    trades: pd.DataFrame,
    *,
    gap_ns: int = GAP_THRESHOLD_NS,
    tolerance_ns: int = CLOCK_TOLERANCE_NS,
) -> dict[str, Any]:
    frames = []
    if not quotes.empty:
        frames.append(quotes[["ts_event", "ts_init"]])
    if not trades.empty:
        frames.append(trades[["ts_event", "ts_init"]])
    if not frames:
        return {
            "events": 0,
            "gaps": 0,
            "gap_ratio": None,
            "crossed_books": 0,
            "crossed_ratio": None,
            "time_inversions": 0,
            "coverage": 0.0,
            "passed": False,
            "reason": "没有行情事件",
        }
    events = pd.concat(frames, ignore_index=True).sort_values("ts_init")
    deltas = events["ts_init"].diff().dropna()
    gaps = int((deltas > gap_ns).sum())
    inversions = int((events["ts_event"] - events["ts_init"] > tolerance_ns).sum())
    crossed = 0
    if not quotes.empty:
        crossed = int((quotes["bid"] > quotes["ask"]).sum())
    span = int(events["ts_init"].iloc[-1] - events["ts_init"].iloc[0])
    if span <= 0:
        coverage = 1.0 if len(events) else 0.0
    else:
        minutes = events["ts_init"] // 60_000_000_000
        coverage = float(minutes.nunique()) / max(span / 60_000_000_000, 1.0)
        coverage = min(coverage, 1.0)
    event_count = int(len(events))
    quote_count = int(len(quotes))
    gap_ratio = gaps / event_count
    crossed_ratio = (crossed / quote_count) if quote_count else 0.0
    passed = gap_ratio <= 0.001 and crossed_ratio <= 0.001 and inversions == 0 and event_count > 0
    return {
        "events": event_count,
        "quotes": quote_count,
        "trades": int(len(trades)),
        "gaps": gaps,
        "gap_ratio": gap_ratio,
        "crossed_books": crossed,
        "crossed_ratio": crossed_ratio,
        "time_inversions": inversions,
        "coverage": coverage,
        "passed": passed,
        "thresholds": {
            "gap_ns": gap_ns,
            "clock_tolerance_ns": tolerance_ns,
            "max_gap_ratio": 0.001,
            "max_crossed_ratio": 0.001,
        },
    }


def _opponent(quotes: pd.DataFrame, ts_init: int, side: str) -> Decimal | None:
    if quotes.empty:
        return None
    later = quotes[quotes["ts_init"] >= ts_init]
    if later.empty:
        return None
    row = later.iloc[0]
    return row["ask"] if side == "BUY" else row["bid"]


def build_fill_book(fills: list[dict[str, Any]], quotes: pd.DataFrame, latency_ns: int) -> pd.DataFrame:
    rows = []
    for fill in fills:
        tags = _tag_map(fill.get("tags") or [])
        quantity = Decimal(fill["quantity"])
        price = Decimal(fill["price"])
        notional = quantity * price
        commission = Decimal(str(fill["commission"]).split()[0])
        decision = int(tags["ts_decision"]) if "ts_decision" in tags else None
        mid = Decimal(tags["mid"]) if "mid" in tags else None
        arrival = None
        if mid and mid > 0 and decision is not None:
            if fill["side"] == "BUY":
                arrival = (price - mid) / mid * Decimal(10000)
            else:
                arrival = (mid - price) / mid * Decimal(10000)
        opponent = None
        slippage = None
        if decision is not None:
            opponent = _opponent(quotes, decision + latency_ns, fill["side"])
            if opponent and opponent > 0:
                if fill["side"] == "BUY":
                    slippage = (price - opponent) / opponent * Decimal(10000)
                else:
                    slippage = (opponent - price) / opponent * Decimal(10000)
        fee_bps = (commission / notional * Decimal(10000)) if notional else None
        rows.append(
            {
                "trade_id": fill["trade_id"],
                "client_order_id": fill["client_order_id"],
                "side": fill["side"],
                "quantity": str(quantity),
                "price": str(price),
                "notional_quote": str(notional),
                "commission": str(commission),
                "liquidity_side": fill["liquidity_side"],
                "ts_event": fill["ts_event"],
                "ts_init": fill["ts_init"],
                "ts_decision": decision,
                "decision_mid": None if mid is None else str(mid),
                "arrival_cost_bps": None if arrival is None else str(arrival),
                "opponent_at_effective": None if opponent is None else str(opponent),
                "realized_slippage_bps": None if slippage is None else str(slippage),
                "fee_bps": None if fee_bps is None else str(fee_bps),
            }
        )
    return pd.DataFrame(rows)


def participation(fills: pd.DataFrame, trades: pd.DataFrame) -> Decimal | None:
    if trades.empty or fills.empty:
        return None
    market_qty = sum((Decimal(str(size)) for size in trades["size"]), Decimal(0))
    our_qty = sum((Decimal(quantity) for quantity in fills["quantity"]), Decimal(0))
    if market_qty <= 0:
        return None
    return our_qty / market_qty


def equity_curve(
    quotes: pd.DataFrame,
    fills: list[dict[str, Any]],
    starting_usdt: Decimal,
) -> pd.Series:
    cash = starting_usdt
    base = Decimal(0)
    events: list[tuple[int, Decimal]] = []
    ordered = sorted(fills, key=lambda item: item["ts_init"])
    quote_rows = quotes.to_dict("records") if not quotes.empty else []
    quote_index = 0
    mid = None
    for fill in ordered:
        while quote_index < len(quote_rows) and quote_rows[quote_index]["ts_init"] <= fill["ts_init"]:
            row = quote_rows[quote_index]
            mid = (row["bid"] + row["ask"]) / 2
            quote_index += 1
        quantity = Decimal(fill["quantity"])
        price = Decimal(fill["price"])
        commission = Decimal(str(fill["commission"]).split()[0])
        if fill["side"] == "BUY":
            cash -= quantity * price + commission
            base += quantity
        else:
            cash += quantity * price - commission
            base -= quantity
        mark = mid if mid is not None else price
        events.append((fill["ts_init"], cash + base * mark))
    if not events and not quotes.empty:
        row = quotes.iloc[-1]
        mid = (row["bid"] + row["ask"]) / 2
        events.append((int(row["ts_init"]), starting_usdt))
    if not events:
        return pd.Series(dtype="float64")
    index = pd.to_datetime([item[0] for item in events], unit="ns", utc=True)
    return pd.Series([float(item[1]) for item in events], index=index, name="equity")


def write_daily_summary(
    path: Path,
    equity: pd.Series,
    fills: pd.DataFrame,
    starting_usdt: Decimal,
) -> pd.Series:
    path.parent.mkdir(parents=True, exist_ok=True)
    if equity.empty:
        end = starting_usdt
        daily = pd.Series(dtype="float64")
    else:
        end = Decimal(str(equity.iloc[-1]))
        daily_equity = equity.resample("1D").last().dropna()
        daily = daily_equity.pct_change().dropna()
    fees = Decimal(0)
    if not fills.empty:
        fees = sum((Decimal(value) for value in fills["commission"]), Decimal(0))
    lines = [
        "通路测试 日收益摘要",
        f"起始权益 {starting_usdt} USDT",
        f"结束权益 {end} USDT",
        f"净盈亏 {end - starting_usdt} USDT",
        f"费用合计 {fees} USDT",
        f"成交笔数 {0 if fills.empty else len(fills)}",
        f"日收益点数 {len(daily)}",
    ]
    if len(daily):
        lines.append("日收益")
        for stamp, value in daily.items():
            lines.append(f"{stamp.date().isoformat()} {value:.8f}")
    else:
        lines.append("日收益点数少于 2，不能做成年化夏普。这一页就是等价的日收益摘要。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not daily.empty:
        daily.to_csv(path.with_suffix(".csv"), header=["return"])
    return daily


def write_quantstats(daily: pd.Series, path: Path) -> str | None:
    if len(daily) < 2:
        return "日收益少于两个点，QuantStats 没有可计算的样本。已写日收益摘要。"
    try:
        import quantstats
    except ImportError:
        return "没有安装 QuantStats。已写日收益摘要，缺口是 HTML 里的夏普、回撤和月度表。"
    try:
        quantstats.reports.html(
            daily,
            benchmark=None,
            title="通路测试",
            output=str(path),
            periods_per_year=365,
            download_filename=path.name,
        )
    except Exception as exc:
        return (
            "QuantStats 没有生成 HTML"
            f"（{type(exc).__name__}）。已写日收益摘要，缺口是 HTML 里的夏普、回撤和月度表。"
        )
    return None


def write_fill_book(frame: pd.DataFrame, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / "fill_book.csv"
    frame.to_csv(csv_path, index=False)
    table = pa.Table.from_pandas(frame, preserve_index=False)
    pq.write_table(table, directory / "fill_book.parquet")


def _replay_reason(replay_diff: int | None, live_diff: int | None) -> str:
    if replay_diff == 0 and live_diff == 0:
        return "同一段行情重放两次，价和量一致，并且和该次会话的影子账本一致。"
    if replay_diff == 0 and live_diff is None:
        return "同一段行情重放两次，价和量一致。这次没有影子账本可对照。"
    if replay_diff == 0:
        return "同一段行情重放两次价量一致，但和该次会话影子账本还有差异。"
    return "同一段行情重放两次，价和量应一致，并且和该次会话的影子账本一致。"


def acceptance(
    *,
    quality: dict[str, Any],
    fill_book: pd.DataFrame,
    participation_rate: Decimal | None,
    replay_diff: int | None,
    live_diff: int | None = None,
    session_seconds: int,
) -> dict[str, Any]:
    has_numbers = (
        not fill_book.empty
        and fill_book["arrival_cost_bps"].notna().all()
        and fill_book["fee_bps"].notna().all()
        and participation_rate is not None
    )
    return {
        "continuous_24h_and_resume": {
            "met": False,
            "reason": (
                "需要连续覆盖加密市场 24 小时，进程中断后从 Parquet 和影子账本恢复且不重复记账。"
                f"这次会话配置时长是 {session_seconds} 秒。恢复和去重已经有账本实现。"
            ),
        },
        "disconnect_does_not_fill_old_orders": {
            "met": False,
            "mechanism_tested": True,
            "reason": "断线前的旧单不会用断线窗口里补出来的价格成交，过期行情也不进撮合。这一条有回归测试。还没有在一段真实行情里拔线。",
        },
        "report_header": {
            "met": True,
            "reason": "报告抬头写入配置哈希、费率版本和延迟参数。",
        },
        "replay_diff_zero": {
            "met": replay_diff == 0 and live_diff in (None, 0),
            "replay_diff": replay_diff,
            "live_vs_replay_diff": live_diff,
            "reason": _replay_reason(replay_diff, live_diff),
        },
        "participation_fee_arrival": {
            "met": bool(has_numbers),
            "participation": None if participation_rate is None else str(participation_rate),
            "reason": "有成交时，参与率、费用基点和到达成本都要是数。",
        },
        "no_exchange_execution_client": {
            "met": True,
            "reason": "进程启动前拒绝交易所执行客户端、交易密钥和代理。",
        },
        "data_quality_passed": quality.get("passed"),
    }
