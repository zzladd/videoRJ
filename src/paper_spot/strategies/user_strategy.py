"""自己的买卖写在这个文件的 decide 里。

默认的 `paper_spot run` 不读这个文件，仍用连通性策略。
要启用这里的函数：

    PYTHONPATH=src python3 -m paper_spot run --strategy user

函数签名：

    def decide(market: MarketView) -> OrderIntent | None

market 是已经到达的最优买卖价和基础币持仓。返回 OrderIntent 表示要买或卖，
返回 None 表示这口行情不下单。名义金额超过配置上限的买单会被拒绝。
不要在这个文件里读取交易密钥，也不要连接交易所。
"""

from decimal import Decimal

from paper_spot.strategies.api import MarketView
from paper_spot.strategies.api import OrderIntent
from paper_spot.strategies.connectivity import connectivity_decide


def decide(market: MarketView) -> OrderIntent | None:
    # 这一行和默认连通性策略相同，不平衡倍数写死为 1.2。
    # 换成自己的买卖时，删掉这一行，改用 market 里的买卖价和持仓。
    return connectivity_decide(market, Decimal("1.2"))
