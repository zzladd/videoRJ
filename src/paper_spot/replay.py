"""用同一套撮合配置重放行情，比较成交价和数量。"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.engine import BacktestEngineConfig
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.backtest.models import MakerTakerFeeModel
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.core.data import Data
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.enums import RecordFlag
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Money

from paper_spot.sandbox_client import static_latency_model
from paper_spot.strategy import ConnectivityConfig
from paper_spot.strategy import ConnectivityStrategy


def _add_data(engine: BacktestEngine, data: list[Data]) -> None:
    """按类型分批加入。引擎只根据每批的第一条判断有没有订单簿。"""
    grouped: dict[type, list[Data]] = {}
    for item in data:
        grouped.setdefault(type(item), []).append(item)

    def rank(kind: type) -> int:
        name = kind.__name__
        if name.startswith("OrderBook"):
            return 0
        if name == "TradeTick":
            return 1
        return 2

    for kind in sorted(grouped, key=rank):
        batch = sorted(grouped[kind], key=lambda item: item.ts_init)
        engine.add_data(batch)


def restore_book_batches(deltas: list[OrderBookDelta]) -> list[OrderBookDeltas]:
    """把 Parquet 里的逐条档位还原成当时送进撮合的订单簿包。

    目录按单条 OrderBookDelta 落盘。币安 REST 初始快照的最后一档没有 F_LAST，
    OrderBookDeltas.batch() 会把它和下一包粘在一起，整包的 ts_init 变成下一包
    的到达时间。第一笔 IOC 的 100 毫秒延迟到期时盘口还是空的，订单被取消，
    后面的名额被替补单占掉，会话里的最后一笔卖出就对不上。
    同一接收时间的档位仍是一包；遇到 F_LAST 也收包。
    """
    if not deltas:
        return []
    ordered = sorted(deltas, key=lambda delta: delta.ts_init)
    groups: list[list[OrderBookDelta]] = []
    current: list[OrderBookDelta] = []
    for delta in ordered:
        if current and (
            delta.ts_init != current[-1].ts_init or delta.instrument_id != current[-1].instrument_id
        ):
            groups.append(current)
            current = []
        current.append(delta)
        if delta.flags & RecordFlag.F_LAST:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return [OrderBookDeltas(instrument_id=group[0].instrument_id, deltas=group) for group in groups]


def _data_sort_key(item: Data) -> tuple[int, int]:
    name = type(item).__name__
    if name in {"OrderBookDeltas", "OrderBookDelta", "OrderBookDepth10"}:
        rank = 0
    elif name == "TradeTick":
        rank = 1
    else:
        rank = 2
    return (item.ts_init, rank)


def fill_keys(engine: BacktestEngine) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for order in engine.cache.orders():
        for event in order.events:
            if type(event).__name__ != "OrderFilled":
                continue
            rows.append((event.order_side.name, str(event.last_qty), str(event.last_px)))
    return rows


def diff_count(left: list[tuple[str, str, str]], right: list[tuple[str, str, str]]) -> int:
    mismatches = abs(len(left) - len(right))
    for first, second in zip(left, right):
        if first != second:
            mismatches += 1
    return mismatches


def run_backtest(
    instrument: Instrument,
    data: list[Data],
    config: ConnectivityConfig,
    starting_balances: list[str] | None = None,
) -> list[tuple[str, str, str]]:
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="PAPER-001",
            logging=LoggingConfig(log_level="ERROR"),
        )
    )
    try:
        engine.add_venue(
            venue=instrument.id.venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            starting_balances=[Money.from_str(item) for item in (starting_balances or ["10000 USDT"])],
            base_currency=None,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            latency_model=static_latency_model(config.latency_ns),
            book_type=BookType.L2_MBP,
            trade_execution=True,
            liquidity_consumption=True,
            bar_execution=False,
            use_message_queue=True,
        )
        engine.add_instrument(instrument)
        _add_data(engine, data)
        engine.add_strategy(ConnectivityStrategy(config))
        engine.run()
        return fill_keys(engine)
    finally:
        engine.dispose()


def replay_diff(instrument: Instrument, data: list[Data], config: ConnectivityConfig) -> int:
    first = run_backtest(instrument, data, config)
    second = run_backtest(instrument, data, config)
    return diff_count(first, second)
