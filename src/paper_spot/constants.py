"""撮合常量。报告和沙箱共用这一处，避免策略里另写一套费率或延迟。"""

from __future__ import annotations

LATENCY_NS = 100_000_000
STALE_AFTER_NS = 2_000_000_000
GAP_THRESHOLD_NS = 2_000_000_000
CLOCK_TOLERANCE_NS = 500_000_000

OFFICIAL_HOSTS = frozenset(
    {
        "api.binance.com",
        "data-api.binance.vision",
        "data.binance.vision",
        "stream.binance.com",
        "data-stream.binance.vision",
        "api.bybit.com",
        "stream.bybit.com",
        "www.okx.com",
        "ws.okx.com",
        "okx.com",
    }
)

FORBIDDEN_ENV = (
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "BINANCE_DEMO_API_KEY",
    "BINANCE_DEMO_API_SECRET",
    "BYBIT_API_KEY",
    "BYBIT_API_SECRET",
    "BYBIT_TESTNET_API_KEY",
    "BYBIT_TESTNET_API_SECRET",
    "OKX_API_KEY",
    "OKX_API_SECRET",
    "OKX_API_PASSPHRASE",
    "OKX_DEMO_API_KEY",
    "OKX_DEMO_API_SECRET",
    "OKX_DEMO_API_PASSPHRASE",
)
