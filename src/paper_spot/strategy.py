"""连通性策略。名义金额远小于盘口，只用来产生影子成交样本。"""

from __future__ import annotations

from decimal import Decimal
from decimal import ROUND_DOWN
from decimal import ROUND_UP
from pathlib import Path

from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

from paper_spot.constants import LATENCY_NS
from paper_spot.ledger import ShadowLedger


class ConnectivityConfig(StrategyConfig, frozen=True):
    instrument_id: str
    order_quote_notional: str = "12"
    max_notional: str = "50"
    imbalance_multiple: str = "1.2"
    protection_band: str = "0.005"
    cooldown_seconds: float = 5.0
    max_orders: int = 6
    session_seconds: int = 0
    book_depth: int = 20
    book_update_speed_ms: int = 100
    use_exchange_depth_params: bool = False
    maker_fee: str = "0"
    taker_fee: str = "0"
    fee_version: str = ""
    ledger_path: str = ""
    starting_balance: str = "10000 USDT"
    latency_ns: int = LATENCY_NS


class ConnectivityStrategy(Strategy):
    """买一量达到卖一量的设定倍数时买入一小笔，反向时卖掉持仓。"""

    def __init__(self, config: ConnectivityConfig) -> None:
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self._last_decision_ns: int | None = None
        self._orders_sent = 0
        self._ready = False
        self.ledger: ShadowLedger | None = None

    def on_start(self) -> None:
        maker = Decimal(self.config.maker_fee)
        taker = Decimal(self.config.taker_fee)
        if maker <= 0 or taker <= 0:
            self.log.error("费率配置缺失，不开新仓")
            return
        if self.config.ledger_path:
            self.ledger = ShadowLedger(
                path=Path(self.config.ledger_path),
                starting_balances=[self.config.starting_balance],
            )
        self._ready = True
        params = None
        if self.config.use_exchange_depth_params:
            params = {"update_speed": self.config.book_update_speed_ms}
        self.subscribe_trade_ticks(self.instrument_id)
        self.subscribe_quote_ticks(self.instrument_id)
        self.subscribe_order_book_deltas(
            self.instrument_id,
            book_type=BookType.L2_MBP,
            depth=self.config.book_depth,
            params=params,
        )
        if self.config.session_seconds > 0:
            alert_ns = self.clock.timestamp_ns() + int(self.config.session_seconds * 1_000_000_000)
            self.clock.set_time_alert_ns(
                name="session_end",
                alert_time_ns=alert_ns,
                callback=self._end_session,
            )
        self.log.info(
            "通路测试启动 "
            f"instrument={self.instrument_id} "
            f"notional={self.config.order_quote_notional} "
            f"imbalance={self.config.imbalance_multiple} "
            f"fee={self.config.fee_version}"
        )

    def _end_session(self, event) -> None:
        del event
        self.log.info("通路测试到达会话时长，停止")
        self.shutdown_system("通路测试会话结束")

    def on_quote_tick(self, tick) -> None:
        if not self._ready or tick.instrument_id != self.instrument_id:
            return
        from paper_spot import runtime

        if runtime.CURRENT is not None and runtime.CURRENT.gate.blocks(tick.ts_event, tick.ts_init):
            return
        if self.cache.orders_inflight(instrument_id=self.instrument_id):
            return
        if self._orders_sent >= self.config.max_orders:
            return
        cooldown_ns = int(self.config.cooldown_seconds * 1_000_000_000)
        if (
            self._last_decision_ns is not None
            and self.clock.timestamp_ns() - self._last_decision_ns < cooldown_ns
        ):
            return

        bid_size = tick.bid_size.as_decimal()
        ask_size = tick.ask_size.as_decimal()
        if bid_size <= 0 or ask_size <= 0:
            return
        if tick.bid_price >= tick.ask_price:
            return
        multiple = Decimal(self.config.imbalance_multiple)
        net = self._base_qty()
        if bid_size >= multiple * ask_size and net <= 0:
            self._send(OrderSide.BUY, tick)
        elif ask_size >= multiple * bid_size and net > 0:
            self._send(OrderSide.SELL, tick)

    def _base_qty(self) -> Decimal:
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            return Decimal(0)
        account = self.portfolio.account(instrument.id.venue)
        if account is None:
            return Decimal(0)
        balance = account.balance_total(instrument.base_currency)
        if balance is None:
            return Decimal(0)
        return balance.as_decimal()

    def _send(self, side: OrderSide, tick) -> None:
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            self.log.warning("品种还没有进入缓存，跳过")
            return
        band = Decimal(self.config.protection_band)
        mid = (tick.bid_price.as_decimal() + tick.ask_price.as_decimal()) / 2
        increment = instrument.price_increment.as_decimal()
        if side == OrderSide.BUY:
            raw_limit = mid * (Decimal(1) + band)
            steps = (raw_limit / increment).to_integral_value(rounding=ROUND_DOWN)
            quantity = self._buy_qty(instrument, tick.ask_price.as_decimal())
        else:
            raw_limit = mid * (Decimal(1) - band)
            steps = (raw_limit / increment).to_integral_value(rounding=ROUND_UP)
            quantity = self._sell_qty(instrument)
        if quantity is None or steps <= 0:
            return
        limit = instrument.make_price(str(steps * increment))
        if side == OrderSide.BUY and limit < tick.ask_price:
            self.log.info("保护带低于卖一，剩余不成交，跳过这次买入")
            return
        if side == OrderSide.SELL and limit > tick.bid_price:
            self.log.info("保护带高于买一，剩余不成交，跳过这次卖出")
            return
        min_notional = instrument.min_notional.as_decimal() if instrument.min_notional else Decimal(0)
        touch = tick.ask_price.as_decimal() if side == OrderSide.BUY else tick.bid_price.as_decimal()
        if quantity.as_decimal() * touch < min_notional:
            self.log.info("名义金额低于品种最小成交额，跳过")
            return
        order = self.order_factory.limit(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=quantity,
            price=limit,
            time_in_force=TimeInForce.IOC,
            tags=[
                f"ts_decision={tick.ts_init}",
                f"mid={mid}",
                f"bid={tick.bid_price}",
                f"ask={tick.ask_price}",
                "title=通路测试",
            ],
        )
        self._orders_sent += 1
        self._last_decision_ns = self.clock.timestamp_ns()
        if self.ledger is not None:
            self.ledger.remember_order(
                {
                    "client_order_id": order.client_order_id.value,
                    "side": side.name,
                    "quantity": str(quantity),
                    "price": str(limit),
                    "time_in_force": "IOC",
                    "ts_decision": tick.ts_init,
                    "order_ts_init": order.ts_init,
                    "mid": str(mid),
                    "bid": str(tick.bid_price),
                    "ask": str(tick.ask_price),
                }
            )
        self.log.info(f"通路测试提交 {side.name} {quantity} @ {limit} IOC")
        self.submit_order(order)

    def _buy_qty(self, instrument, ask: Decimal) -> Quantity | None:
        notional = Decimal(self.config.order_quote_notional)
        ceiling = Decimal(self.config.max_notional)
        if notional <= 0 or notional > ceiling:
            self.log.error("下单名义金额超出配置上限，拒绝")
            return None
        raw = notional / ask
        quantity = instrument.make_qty(str(raw))
        if quantity.as_decimal() <= 0:
            return None
        min_notional = instrument.min_notional.as_decimal() if instrument.min_notional else Decimal(0)
        step = instrument.size_increment.as_decimal()
        while quantity.as_decimal() * ask < min_notional:
            quantity = instrument.make_qty(str(quantity.as_decimal() + step))
            if quantity.as_decimal() * ask > ceiling:
                self.log.warning("抬到最小名义金额后会超过上限，跳过")
                return None
        if quantity.as_decimal() * ask > ceiling:
            return None
        return quantity

    def _sell_qty(self, instrument) -> Quantity | None:
        available = self._base_qty()
        if available <= 0:
            return None
        return instrument.make_qty(str(available))

    def on_order_filled(self, event: OrderFilled) -> None:
        if self.ledger is None:
            return
        order = self.cache.order(event.client_order_id)
        tags = list(order.tags) if order is not None else []
        balances = self._balance_map()
        fill = {
            "trade_id": event.trade_id.value,
            "client_order_id": event.client_order_id.value,
            "side": event.order_side.name,
            "quantity": str(event.last_qty),
            "price": str(event.last_px),
            "commission": str(event.commission),
            "liquidity_side": event.liquidity_side.name,
            "ts_event": event.ts_event,
            "ts_init": event.ts_init,
            "tags": tags,
        }
        self.ledger.apply_fill(fill, balances)
        self.log.info(f"影子成交 {fill['side']} {fill['quantity']} @ {fill['price']} 费用 {fill['commission']}")

    def _balance_map(self) -> dict[str, str]:
        instrument = self.cache.instrument(self.instrument_id)
        if instrument is None:
            return {}
        account = self.portfolio.account(instrument.id.venue)
        if account is None:
            return {}
        balances: dict[str, str] = {}
        for currency in (instrument.quote_currency, instrument.base_currency):
            money = account.balance_total(currency)
            if money is not None:
                balances[currency.code] = str(money.as_decimal())
        return balances

    def on_stop(self) -> None:
        if self.cache.instrument(self.instrument_id) is not None:
            self.cancel_all_orders(self.instrument_id)
        if self.ledger is not None:
            self.ledger.flush()
