"""默认连通性策略：买一量够大就买一小笔，反向就卖掉持仓。"""

from __future__ import annotations

from decimal import Decimal

from paper_spot.strategies.api import MarketView
from paper_spot.strategies.api import OrderIntent


def connectivity_decide(market: MarketView, imbalance_multiple: Decimal) -> OrderIntent | None:
    if market.bid_size <= 0 or market.ask_size <= 0:
        return None
    if market.bid >= market.ask:
        return None
    if market.bid_size >= imbalance_multiple * market.ask_size and market.base_qty <= 0:
        return OrderIntent(side="BUY")
    if market.ask_size >= imbalance_multiple * market.bid_size and market.base_qty > 0:
        return OrderIntent(side="SELL")
    return None
