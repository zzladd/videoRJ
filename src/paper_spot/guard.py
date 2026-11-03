"""启动自检。发现交易所执行客户端、交易密钥或代理就拒绝启动。"""

from __future__ import annotations

import os
from typing import Any

from paper_spot.config_loader import ConfigError, assert_official_url
from paper_spot.constants import FORBIDDEN_ENV

EXCHANGE_EXEC_MODULES = (
    "nautilus_trader.adapters.binance",
    "nautilus_trader.adapters.bybit",
    "nautilus_trader.adapters.okx",
)


class ShadowGuardError(RuntimeError):
    pass


def assert_no_trading_secrets() -> None:
    present = [name for name in FORBIDDEN_ENV if os.environ.get(name)]
    if present:
        joined = ", ".join(present)
        raise ShadowGuardError(f"影子盘进程里有交易密钥环境变量：{joined}")


def assert_data_config(config: Any) -> None:
    api_key = getattr(config, "api_key", None)
    api_secret = getattr(config, "api_secret", None)
    passphrase = getattr(config, "api_passphrase", None)
    if api_key or api_secret or passphrase:
        raise ShadowGuardError("行情配置里写了 API 密钥。第一期公共行情不配置密钥。")
    proxy_url = getattr(config, "proxy_url", None)
    if proxy_url:
        raise ShadowGuardError("行情配置里写了代理。影子盘只连接官方域名。")
    assert_official_url(getattr(config, "base_url_http", None))
    assert_official_url(getattr(config, "base_url_ws", None))


def assert_exec_config(config: Any) -> None:
    from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig

    if type(config) is not SandboxExecutionClientConfig:
        raise ShadowGuardError(
            f"执行配置必须是沙箱 SandboxExecutionClientConfig，实际是 {type(config).__name__}"
        )
    if str(config.account_type).upper() != "CASH":
        raise ShadowGuardError("第一期账户必须是现货现金账户")
    if config.trade_execution is not True:
        raise ShadowGuardError("trade_execution 必须打开")
    if str(config.book_type) != "L2_MBP":
        raise ShadowGuardError("沙箱订单簿必须是 L2_MBP，才能按有限档吃单")
    if config.bar_execution:
        raise ShadowGuardError("第一期不用 K 线成交")


def assert_execution_client(client: Any) -> None:
    from paper_spot.sandbox_client import PaperSandboxExecutionClient

    module = type(client).__module__
    name = type(client).__name__
    if module.startswith(EXCHANGE_EXEC_MODULES) or (
        "ExecutionClient" in name and "Sandbox" not in name
    ):
        raise ShadowGuardError(f"拒绝启动：注册了交易所执行客户端 {module}.{name}")
    if type(client) is not PaperSandboxExecutionClient:
        raise ShadowGuardError(
            "拒绝启动：执行客户端不是带 100 毫秒延迟和盘口消耗的沙箱客户端"
        )
    exchange = client.exchange
    if not exchange.use_message_queue:
        raise ShadowGuardError("沙箱消息队列没有打开，100 毫秒延迟不会生效")
    if exchange.latency_model.insert_latency_nanos != 100_000_000:
        raise ShadowGuardError("下单延迟不是 100 毫秒")
    if exchange.latency_model.cancel_latency_nanos != 100_000_000:
        raise ShadowGuardError("撤单延迟不是 100 毫秒")
    if not exchange.trade_execution:
        raise ShadowGuardError("撮合没有打开 trade_execution")
    if not exchange.liquidity_consumption:
        raise ShadowGuardError("撮合没有打开盘口消耗")
    if exchange.fee_model.__class__.__name__ != "MakerTakerFeeModel":
        raise ShadowGuardError("费率模型不是 MakerTakerFeeModel")


def assert_built_node(node: Any) -> None:
    clients = list(node.kernel.exec_engine._clients.values())
    if not clients:
        raise ShadowGuardError("没有注册沙箱执行客户端")
    for client in clients:
        assert_execution_client(client)
    factories = getattr(node._builder, "_exec_factories", {})
    for name, factory in factories.items():
        if "Sandbox" not in factory.__name__:
            raise ShadowGuardError(f"执行工厂 {name} 不是沙箱工厂")
