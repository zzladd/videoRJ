"""组装只读行情加沙箱撮合的交易节点。不注册任何交易所执行客户端。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from nautilus_trader.adapters.binance.common.enums import BinanceAccountType
from nautilus_trader.adapters.binance.config import BinanceDataClientConfig
from nautilus_trader.adapters.binance.config import BinanceInstrumentProviderConfig
from nautilus_trader.adapters.binance.factories import BinanceLiveDataClientFactory
from nautilus_trader.adapters.bybit.config import BybitDataClientConfig
from nautilus_trader.adapters.bybit.factories import BybitLiveDataClientFactory
from nautilus_trader.adapters.okx.config import OKXDataClientConfig
from nautilus_trader.adapters.okx.factories import OKXLiveDataClientFactory
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.common.config import ImportableActorConfig
from nautilus_trader.common.config import InstrumentProviderConfig
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.core.nautilus_pyo3 import BybitProductType
from nautilus_trader.core.nautilus_pyo3 import OKXInstrumentType
from nautilus_trader.live.config import LiveExecEngineConfig
from nautilus_trader.live.config import LiveRiskEngineConfig
from nautilus_trader.live.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.persistence.config import StreamingConfig
from nautilus_trader.trading.config import ImportableStrategyConfig

from paper_spot import runtime
from paper_spot.guard import assert_built_node
from paper_spot.guard import assert_data_config
from paper_spot.guard import assert_exec_config
from paper_spot.sandbox_client import SandboxLiveExecClientFactory
from paper_spot.venue_probe import VenueChoice

DATA_FACTORIES = {
    "BINANCE": BinanceLiveDataClientFactory,
    "BYBIT": BybitLiveDataClientFactory,
    "OKX": OKXLiveDataClientFactory,
}


def data_client_config(choice: VenueChoice):
    instrument_id = InstrumentId.from_str(choice.instrument_id)
    provider = InstrumentProviderConfig(load_all=False, load_ids=frozenset([instrument_id]))
    if choice.venue == "BINANCE":
        config = BinanceDataClientConfig(
            api_key=None,
            api_secret=None,
            account_type=BinanceAccountType.SPOT,
            us=False,
            base_url_http=choice.http_base,
            base_url_ws=choice.ws_base,
            instrument_provider=BinanceInstrumentProviderConfig(
                load_all=False,
                load_ids=frozenset([instrument_id]),
                query_commission_rates=False,
            ),
        )
    elif choice.venue == "BYBIT":
        config = BybitDataClientConfig(
            api_key=None,
            api_secret=None,
            product_types=(BybitProductType.SPOT,),
            base_url_http=choice.http_base,
            instrument_provider=provider,
        )
    elif choice.venue == "OKX":
        config = OKXDataClientConfig(
            api_key=None,
            api_secret=None,
            api_passphrase=None,
            instrument_types=(OKXInstrumentType.SPOT,),
            base_url_http=choice.http_base,
            base_url_ws=choice.ws_base,
            instrument_provider=provider,
        )
    else:
        raise RuntimeError(f"没有 {choice.venue} 的现货行情配置")
    assert_data_config(config)
    return config


def exec_client_config(choice: VenueChoice, starting_balances: list[str]) -> SandboxExecutionClientConfig:
    config = SandboxExecutionClientConfig(
        venue=choice.venue,
        starting_balances=starting_balances,
        base_currency=None,
        oms_type="NETTING",
        account_type="CASH",
        book_type="L2_MBP",
        trade_execution=True,
        bar_execution=False,
    )
    assert_exec_config(config)
    return config


def build_node(
    *,
    run_dir: Path,
    paper: dict,
    fee: dict,
    choice: VenueChoice,
    starting_balances: list[str],
    session_seconds: int,
    imbalance: str,
    log_level: str = "INFO",
) -> TradingNode:
    if runtime.CURRENT is None:
        raise RuntimeError("费率会话还没有准备好，拒绝建节点")
    data_config = data_client_config(choice)
    sandbox_config = exec_client_config(choice, starting_balances)
    catalog_path = str((run_dir / "catalog").resolve())
    stream_path = str((run_dir / "stream").resolve())
    (run_dir / "catalog").mkdir(parents=True, exist_ok=True)
    (run_dir / "stream").mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    max_notional = int(Decimal(paper["max_notional_per_order"]))
    strategy_config = {
        "instrument_id": choice.instrument_id,
        "order_quote_notional": str(paper["order_quote_notional"]),
        "max_notional": str(paper["max_notional_per_order"]),
        "imbalance_multiple": str(imbalance),
        "protection_band": str(paper["protection_band"]),
        "cooldown_seconds": float(paper["cooldown_seconds"]),
        "max_orders": int(paper["max_orders"]),
        "session_seconds": int(session_seconds),
        "book_depth": int(paper["book_depth"]),
        "book_update_speed_ms": int(paper["book_update_speed_ms"]),
        "use_exchange_depth_params": True,
        "maker_fee": str(fee["maker_rate"]),
        "taker_fee": str(fee["taker_rate"]),
        "fee_version": str(fee["fee_version"]),
        "ledger_path": str((run_dir / "ledger.json").resolve()),
        "starting_balance": str(paper["starting_balance"]),
        "latency_ns": 100_000_000,
    }
    recorder_config = {
        "instrument_id": choice.instrument_id,
        "catalog_path": catalog_path,
        "book_depth": int(paper["book_depth"]),
        "book_update_speed_ms": int(paper["book_update_speed_ms"]),
        "use_exchange_depth_params": True,
    }
    node_config = TradingNodeConfig(
        trader_id="PAPER-001",
        logging=LoggingConfig(
            log_level=log_level,
            log_level_file=log_level,
            log_directory=str((run_dir / "logs").resolve()),
            log_file_name="paper-spot",
        ),
        risk_engine=LiveRiskEngineConfig(
            max_notional_per_order={choice.instrument_id: max_notional},
            max_order_submit_rate="6/00:00:10",
        ),
        exec_engine=LiveExecEngineConfig(reconciliation=False),
        streaming=StreamingConfig(
            catalog_path=stream_path,
            flush_interval_ms=1000,
            include_types=[QuoteTick, TradeTick, OrderBookDeltas],
        ),
        data_clients={choice.venue: data_config},
        exec_clients={choice.venue: sandbox_config},
        actors=[
            ImportableActorConfig(
                actor_path="paper_spot.recorder:TapeRecorder",
                config_path="paper_spot.recorder:TapeRecorderConfig",
                config=recorder_config,
            )
        ],
        strategies=[
            ImportableStrategyConfig(
                strategy_path="paper_spot.strategy:ConnectivityStrategy",
                config_path="paper_spot.strategy:ConnectivityConfig",
                config=strategy_config,
            )
        ],
    )
    node = TradingNode(config=node_config)
    node.add_data_client_factory(choice.venue, DATA_FACTORIES[choice.venue])
    node.add_exec_client_factory(choice.venue, SandboxLiveExecClientFactory)
    node.build()
    assert_built_node(node)
    return node
