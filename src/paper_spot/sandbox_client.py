"""沙箱执行客户端。

Nautilus 1.231 的 SandboxExecutionClient 把延迟写成 0，并且关闭内部队列，
延迟模型不会生效；配置里也没有盘口消耗开关。这里仍继承该类，但自己构造
同一个 SimulatedExchange，填上 100 毫秒静态延迟、MakerTakerFeeModel、
L2 订单簿、trade_execution 和盘口消耗。不连接任何交易所执行客户端。
"""

from __future__ import annotations

import asyncio

from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
from nautilus_trader.backtest.engine import SimulatedExchange
from nautilus_trader.backtest.execution_client import BacktestExecClient
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.backtest.models import LatencyModel
from nautilus_trader.backtest.models import MakerTakerFeeModel
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.component import TestClock
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.core.data import Data
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.live.factories import LiveExecClientFactory
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import InstrumentClose
from nautilus_trader.model.data import InstrumentStatus
from nautilus_trader.model.data import OrderBookDelta
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import OrderBookDepth10
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.enums import account_type_from_str
from nautilus_trader.model.enums import book_type_from_str
from nautilus_trader.model.enums import oms_type_from_str
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.objects import Money
from nautilus_trader.portfolio.base import PortfolioFacade

from paper_spot import runtime
from paper_spot.constants import LATENCY_NS
from paper_spot.fees import stamp_currency_pair

_MARKET = (QuoteTick, TradeTick, OrderBookDelta, OrderBookDeltas, OrderBookDepth10, Bar)


def static_latency_model(delay_ns: int = LATENCY_NS) -> LatencyModel:
    """1.231 里的固定延迟模型叫 LatencyModel。下单、改单、撤单都是 delay_ns。"""
    return LatencyModel(
        base_latency_nanos=0,
        insert_latency_nanos=delay_ns,
        update_latency_nanos=delay_ns,
        cancel_latency_nanos=delay_ns,
    )


class PaperSandboxExecutionClient(SandboxExecutionClient):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        portfolio: PortfolioFacade,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        config: SandboxExecutionClientConfig,
    ) -> None:
        sandbox_venue = Venue(config.venue)
        oms_type = oms_type_from_str(config.oms_type)
        account_type = account_type_from_str(config.account_type)
        if account_type != AccountType.CASH:
            raise RuntimeError("第一期沙箱只接受现金账户")
        if oms_type != OmsType.NETTING:
            raise RuntimeError("第一期沙箱使用净持仓")
        base_currency = Currency.from_str(config.base_currency) if config.base_currency else None
        self.test_clock = TestClock()

        LiveExecutionClient.__init__(
            self,
            loop=loop,
            client_id=ClientId(config.venue),
            venue=sandbox_venue,
            oms_type=oms_type,
            account_type=account_type,
            base_currency=base_currency,
            instrument_provider=InstrumentProvider(),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=None,
        )
        self._set_account_id(AccountId(f"{config.venue}-001"))
        self.reconciliation_active = False

        self.exchange = SimulatedExchange(
            venue=sandbox_venue,
            oms_type=oms_type,
            account_type=account_type,
            starting_balances=[Money.from_str(balance) for balance in config.starting_balances],
            base_currency=base_currency,
            default_leverage=config.default_leverage,
            leverages=config.leverages or {},
            modules=[],
            portfolio=portfolio,
            msgbus=self._msgbus,
            cache=cache,
            clock=self.test_clock,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            latency_model=static_latency_model(),
            book_type=book_type_from_str(config.book_type),
            frozen_account=config.frozen_account,
            bar_execution=False,
            trade_execution=True,
            liquidity_consumption=True,
            queue_position=False,
            reject_stop_orders=config.reject_stop_orders,
            support_gtd_orders=config.support_gtd_orders,
            support_contingent_orders=config.support_contingent_orders,
            use_position_ids=config.use_position_ids,
            use_random_ids=config.use_random_ids,
            use_reduce_only=config.use_reduce_only,
            use_message_queue=True,
        )
        self._client = BacktestExecClient(
            exchange=self.exchange,
            msgbus=msgbus,
            cache=cache,
            clock=self.test_clock,
        )
        self.exchange.register_client(self._client)
        self.exchange.initialize_account()

    def connect(self) -> None:
        self._log.info("连接沙箱撮合，不连接交易所执行接口")
        self._msgbus.subscribe(f"data.*.{self.venue}.*", handler=self.on_data)
        for instrument in list(self._cache.instruments(venue=self.venue)):
            prepared = self._prepare_instrument(instrument)
            if prepared is not None:
                self.exchange.add_instrument(prepared)
        self._client._set_connected(True)
        self._set_connected(True)
        self._log.info(
            "沙箱已连接 "
            f"latency_ns={self.exchange.latency_model.insert_latency_nanos} "
            f"trade_execution={self.exchange.trade_execution} "
            f"liquidity_consumption={self.exchange.liquidity_consumption} "
            f"book_type={self.exchange.book_type}"
        )

    def _prepare_instrument(self, instrument: Instrument) -> Instrument | None:
        if not isinstance(instrument, CurrencyPair):
            self._log.error(f"第一期拒绝非现货品种 {instrument.id}")
            return None
        session = runtime.CURRENT
        if session is None or session.fee_maker <= 0 or session.fee_taker <= 0:
            self._log.error("费率没有载入，拒绝把品种放进撮合")
            return None
        stamped = stamp_currency_pair(instrument, session.fee_maker, session.fee_taker)
        self._cache.add_instrument(stamped)
        return stamped

    def on_data(self, data: Data) -> None:
        if isinstance(data, Instrument):
            prepared = self._prepare_instrument(data)
            if prepared is None:
                return
            data = prepared
        elif isinstance(data, _MARKET):
            gate = runtime.CURRENT.gate if runtime.CURRENT is not None else None
            if gate is not None and gate.reject(data.ts_event, data.ts_init):
                self._log.warning(
                    f"丢弃过期或断线窗口内的行情，不用于撮合 {type(data).__name__} "
                    f"ts_event={data.ts_event} ts_init={data.ts_init}"
                )
                return

        if isinstance(data, Instrument):
            self.exchange.update_instrument(data)
        elif isinstance(data, InstrumentStatus):
            self.exchange.process_instrument_status(data)
        elif isinstance(data, InstrumentClose):
            self.exchange.process_instrument_close(data)
        elif isinstance(data, OrderBookDelta):
            self.exchange.process_order_book_delta(data)
        elif isinstance(data, OrderBookDeltas):
            self.exchange.process_order_book_deltas(data)
        elif isinstance(data, OrderBookDepth10):
            self.exchange.process_order_book_depth10(data)
        elif isinstance(data, QuoteTick):
            self.exchange.process_quote_tick(data)
        elif isinstance(data, TradeTick):
            self.exchange.process_trade_tick(data)
        elif isinstance(data, Bar):
            return
        self.exchange.process(data.ts_init)


class SandboxLiveExecClientFactory(LiveExecClientFactory):
    """类名必须是这个，交易节点才会把 portfolio 传进来。"""

    @staticmethod
    def create(  # type: ignore[override]
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: SandboxExecutionClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        portfolio: PortfolioFacade,
    ) -> PaperSandboxExecutionClient:
        del name
        return PaperSandboxExecutionClient(
            loop=loop,
            portfolio=portfolio,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
