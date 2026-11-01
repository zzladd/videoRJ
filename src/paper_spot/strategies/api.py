"""策略看到的行情，和它返回的订单意图。

这里没有交易所客户端，也没有密钥。策略只描述想买还是想卖。
名义金额、保护带、冷却和最多下单笔数由外层按配置执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class MarketView:
    """一笔已经到达的最优买卖价，以及当时的基础币持仓。"""

    instrument_id: str
    ts_event: int
    ts_init: int
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    base_qty: Decimal


@dataclass(frozen=True)
class OrderIntent:
    """想发出的一笔订单。quote_notional 为空时用配置里的默认名义金额。"""

    side: str
    quote_notional: Decimal | None = None
