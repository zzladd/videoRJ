"""只探测交易所官方接入点，选择现货公共行情。"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from paper_spot.config_loader import ConfigError, assert_official_url


@dataclass(frozen=True)
class ProbeStatus:
    ok: bool
    detail: str


@dataclass(frozen=True)
class VenueChoice:
    venue: str
    instrument_id: str
    http_base: str | None
    ws_base: str | None
    reason: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "venue": self.venue,
            "instrument_id": self.instrument_id,
            "http_base": self.http_base,
            "ws_base": self.ws_base,
            "reason": self.reason,
        }


def http_status(url: str, timeout: float = 6.0) -> ProbeStatus:
    assert_official_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "paper-spot/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return ProbeStatus(response.status == 200, f"HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        return ProbeStatus(False, f"HTTP {exc.code}")
    except Exception as exc:
        return ProbeStatus(False, f"{type(exc).__name__}")


def websocket_status(host: str, port: int, path: str, timeout: float = 6.0) -> ProbeStatus:
    if host not in {
        "stream.binance.com",
        "data-stream.binance.vision",
        "stream.bybit.com",
        "ws.okx.com",
    }:
        raise ConfigError(f"不探测官方表以外的 WebSocket 主机 {host}")
    try:
        context = ssl.create_default_context()
        raw = socket.create_connection((host, port), timeout=timeout)
        secure = context.wrap_socket(raw, server_hostname=host)
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        secure.sendall(request.encode())
        head = secure.recv(120).decode("latin1", errors="replace")
        secure.close()
    except Exception as exc:
        return ProbeStatus(False, type(exc).__name__)
    return ProbeStatus("101" in head, "WebSocket 101" if "101" in head else "握手未升级")


def choose_venue(status: dict[str, ProbeStatus]) -> VenueChoice:
    """按探测结果选择场所。调用方只传入官方接入点的结果。"""
    if status.get("binance_primary_http", ProbeStatus(False, "")).ok and status.get(
        "binance_primary_ws", ProbeStatus(False, "")
    ).ok:
        return VenueChoice(
            venue="BINANCE",
            instrument_id="BTCUSDT.BINANCE",
            http_base=None,
            ws_base=None,
            reason="Binance 现货默认官方接入点可用，交易对 BTCUSDT，公共 JSON 行情。",
        )
    if status.get("binance_market_http", ProbeStatus(False, "")).ok and status.get(
        "binance_market_ws", ProbeStatus(False, "")
    ).ok:
        primary = status.get("binance_primary_http", ProbeStatus(False, "未探测"))
        return VenueChoice(
            venue="BINANCE",
            instrument_id="BTCUSDT.BINANCE",
            http_base="https://data-api.binance.vision",
            ws_base="wss://data-stream.binance.vision",
            reason=(
                "Binance 现货交易域名不可达"
                f"（{primary.detail}）。改用 Binance 官方行情专用域名 "
                "data-api.binance.vision 与 data-stream.binance.vision，"
                "仍是 Binance 官方适配器，交易对 BTCUSDT，不使用密钥。"
            ),
        )
    if status.get("bybit_http", ProbeStatus(False, "")).ok and status.get(
        "bybit_ws", ProbeStatus(False, "")
    ).ok:
        return VenueChoice(
            venue="BYBIT",
            instrument_id="BTCUSDT-SPOT.BYBIT",
            http_base=None,
            ws_base=None,
            reason="Binance 官方接入点不可达。改用 Bybit 官方现货适配器，交易对 BTCUSDT。",
        )
    if status.get("okx_http", ProbeStatus(False, "")).ok and status.get(
        "okx_ws", ProbeStatus(False, "")
    ).ok:
        return VenueChoice(
            venue="OKX",
            instrument_id="BTC-USDT.OKX",
            http_base=None,
            ws_base=None,
            reason="Binance 与 Bybit 官方接入点不可达。改用 OKX 官方现货适配器，交易对 BTC-USDT。",
        )
    raise ConfigError("Binance、Bybit、OKX 的官方现货接入点都不可达。没有改用其他行情来源。")


def probe_venues(http: Callable[[str], ProbeStatus] | None = None) -> tuple[VenueChoice, dict[str, str]]:
    http = http or http_status
    checks = {
        "binance_primary_http": http("https://api.binance.com/api/v3/ping"),
        "binance_primary_ws": websocket_status(
            "stream.binance.com", 9443, "/stream?streams=btcusdt@bookTicker"
        ),
        "binance_market_http": http("https://data-api.binance.vision/api/v3/ping"),
        "binance_market_ws": websocket_status(
            "data-stream.binance.vision", 443, "/stream?streams=btcusdt@bookTicker"
        ),
        "bybit_http": http(
            "https://api.bybit.com/v5/market/instruments-info?category=spot&symbol=BTCUSDT"
        ),
        "bybit_ws": websocket_status("stream.bybit.com", 443, "/v5/public/spot"),
        "okx_http": http(
            "https://www.okx.com/api/v5/public/instruments?instType=SPOT&instId=BTC-USDT"
        ),
        "okx_ws": websocket_status("ws.okx.com", 8443, "/ws/v5/public"),
    }
    choice = choose_venue(checks)
    details = {name: item.detail for name, item in checks.items()}
    return choice, details


def fetch_binance_filters(http_base: str) -> dict[str, str] | None:
    assert_official_url(http_base)
    url = f"{http_base.rstrip('/')}/api/v3/exchangeInfo?symbol=BTCUSDT"
    request = urllib.request.Request(url, headers={"User-Agent": "paper-spot/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode())
    except Exception:
        return None
    symbol = payload["symbols"][0]
    filters = {item["filterType"]: item for item in symbol["filters"]}
    price = filters["PRICE_FILTER"]
    lot = filters["LOT_SIZE"]
    notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
    return {
        "price_increment": price["tickSize"],
        "size_increment": lot["stepSize"],
        "min_quantity": lot["minQty"],
        "min_notional": notional["minNotional"] if notional else "",
        "status": symbol["status"],
    }
