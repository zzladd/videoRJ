"""可替换的买卖入口。默认连通性策略不经过 user_strategy.py。"""

from paper_spot.strategies.api import MarketView
from paper_spot.strategies.api import OrderIntent

__all__ = ["MarketView", "OrderIntent"]
