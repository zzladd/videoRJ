"""从官方公开行情补本地 K 线。不接收用户提供的地址，也不使用交易密钥。"""

from __future__ import annotations

import csv
import io
import json
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal

from paper_spot.config_loader import assert_official_url
from paper_spot.research.intervals import interval_ms
from paper_spot.research.store import Bar

MAX_BARS = 50_000
_MAX_BODY = 20_000_000

_BYBIT_INTERVAL = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "1d": "D",
}
_OKX_BAR = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "1d": "1D",
}


class HttpBody:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body


Opener = Callable[[str], HttpBody]


def urlopen_opener(url: str) -> HttpBody:
    assert_official_url(url)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "paper-spot/research", "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            assert_official_url(response.geturl())
            body = response.read(_MAX_BODY + 1)
            if len(body) > _MAX_BODY:
                raise ValueError("行情响应过大")
            return HttpBody(getattr(response, "status", 200), body)
    except urllib.error.HTTPError as exc:
        body = exc.read(_MAX_BODY + 1)
        return HttpBody(exc.code, body[:_MAX_BODY])


def fetch_closed_bars(
    venue: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    now_ms: int,
    opener: Opener | None = None,
) -> list[Bar]:
    """拉取 [start_ms, end_ms] 内已收盘的 K 线。end_ms 按开盘时间包含。"""
    opener = opener or urlopen_opener
    step = interval_ms(interval)
    if end_ms < start_ms:
        raise ValueError("结束时间早于开始时间")
    venue = venue.upper()
    symbol = symbol.upper()
    if venue == "BINANCE":
        bars = _binance_bars(symbol, interval, start_ms, end_ms, opener)
    elif venue == "BYBIT":
        bars = _bybit_bars(symbol, interval, start_ms, end_ms, opener)
    elif venue == "OKX":
        bars = _okx_bars(symbol, interval, start_ms, end_ms, now_ms, opener)
    else:
        raise ValueError(f"研究库还不支持场所 {venue}")
    closed = [bar for bar in bars if bar.open_time + step <= now_ms]
    closed = [bar for bar in closed if start_ms <= bar.open_time <= end_ms]
    merged = {bar.open_time: bar for bar in closed}
    ordered = [merged[key] for key in sorted(merged)]
    if len(ordered) > MAX_BARS:
        raise ValueError(f"一次最多补 {MAX_BARS} 根，请缩小时间范围")
    return ordered


def parse_binance_klines(payload: object) -> list[Bar]:
    if isinstance(payload, dict):
        message = payload.get("msg") or payload
        raise ValueError(f"币安 K 线拒绝：{message}")
    if not isinstance(payload, list):
        raise ValueError("币安 K 线不是数组")
    bars = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 6:
            raise ValueError("币安 K 线行不完整")
        bars.append(_bar_from_binance_row(row))
    return bars


def parse_binance_csv(text: str) -> list[Bar]:
    bars = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].strip():
            continue
        head = row[0].strip()
        if not head.isdigit():
            continue
        if len(row) < 6:
            raise ValueError("币安月度 K 线列不完整")
        bars.append(_bar_from_binance_row(row))
    return bars


def bars_from_binance_zip(blob: bytes) -> list[Bar]:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [name for name in archive.namelist() if name.endswith(".csv")]
        if len(names) != 1:
            raise ValueError("月度压缩包里应有一个 csv")
        info = archive.getinfo(names[0])
        if info.file_size > 30_000_000:
            raise ValueError("月度 K 线解压后过大")
        text = archive.read(names[0]).decode("utf-8")
    return parse_binance_csv(text)


def _binance_bars(symbol: str, interval: str, start_ms: int, end_ms: int, opener: Opener) -> list[Bar]:
    found: dict[int, Bar] = {}
    rest_ranges: list[tuple[int, int]] = []
    for year, month, month_start, month_end in _months(start_ms, end_ms):
        url = (
            "https://data.binance.vision/data/spot/monthly/klines/"
            f"{symbol}/{interval}/{symbol}-{interval}-{year:04d}-{month:02d}.zip"
        )
        response = opener(url)
        if response.status == 404:
            rest_ranges.append((max(start_ms, month_start), min(end_ms, month_end)))
            continue
        if response.status != 200:
            raise ValueError(f"币安月度 K 线 HTTP {response.status}")
        for bar in bars_from_binance_zip(response.body):
            found[bar.open_time] = bar
    for range_start, range_end in _merge_ranges(rest_ranges):
        for bar in _binance_rest(symbol, interval, range_start, range_end, opener):
            found[bar.open_time] = bar
    return list(found.values())


def _binance_rest(symbol: str, interval: str, start_ms: int, end_ms: int, opener: Opener) -> list[Bar]:
    step = interval_ms(interval)
    cursor = start_ms
    bars: list[Bar] = []
    for _ in range(60):
        url = (
            "https://data-api.binance.vision/api/v3/klines"
            f"?symbol={symbol}&interval={interval}&startTime={cursor}&endTime={end_ms}&limit=1000"
        )
        response = opener(url)
        if response.status != 200:
            raise ValueError(f"币安 K 线 HTTP {response.status}")
        batch = parse_binance_klines(json.loads(response.body.decode("utf-8")))
        if not batch:
            break
        bars.extend(batch)
        nxt = batch[-1].open_time + step
        if nxt <= cursor or len(batch) < 1000:
            break
        cursor = nxt
    return bars


def _bybit_bars(symbol: str, interval: str, start_ms: int, end_ms: int, opener: Opener) -> list[Bar]:
    bybit_interval = _BYBIT_INTERVAL[interval]
    cursor_end = end_ms
    found: dict[int, Bar] = {}
    for _ in range(60):
        url = (
            "https://api.bybit.com/v5/market/kline"
            f"?category=spot&symbol={symbol}&interval={bybit_interval}"
            f"&start={start_ms}&end={cursor_end}&limit=1000"
        )
        response = opener(url)
        if response.status != 200:
            raise ValueError(f"Bybit K 线 HTTP {response.status}")
        payload = json.loads(response.body.decode("utf-8"))
        if payload.get("retCode") != 0:
            raise ValueError(f"Bybit K 线拒绝：{payload.get('retMsg')}")
        rows = (payload.get("result") or {}).get("list") or []
        if not rows:
            break
        oldest = None
        for row in rows:
            open_time = _epoch_ms(row[0])
            bar = Bar(
                open_time=open_time,
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=Decimal(str(row[5])),
                quote_volume=Decimal(str(row[6])) if len(row) > 6 else Decimal("0"),
            )
            found[open_time] = bar
            oldest = open_time if oldest is None else min(oldest, open_time)
        if oldest is None or oldest <= start_ms or len(rows) < 1000:
            break
        cursor_end = oldest - 1
    return list(found.values())


def _okx_bars(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    now_ms: int,
    opener: Opener,
) -> list[Bar]:
    inst_id = _okx_inst(symbol)
    bar_name = _OKX_BAR[interval]
    step = interval_ms(interval)
    after = end_ms + step
    found: dict[int, Bar] = {}
    for _ in range(600):
        url = (
            "https://www.okx.com/api/v5/market/history-candles"
            f"?instId={inst_id}&bar={bar_name}&after={after}&limit=100"
        )
        response = opener(url)
        if response.status != 200:
            raise ValueError(f"OKX K 线 HTTP {response.status}")
        payload = json.loads(response.body.decode("utf-8"))
        if payload.get("code") != "0":
            raise ValueError(f"OKX K 线拒绝：{payload.get('msg')}")
        rows = payload.get("data") or []
        if not rows:
            break
        oldest = None
        for row in rows:
            open_time = _epoch_ms(row[0])
            confirmed = True if len(row) < 9 else str(row[8]) == "1"
            if not confirmed and open_time + step > now_ms:
                continue
            bar = Bar(
                open_time=open_time,
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=Decimal(str(row[5])),
                quote_volume=Decimal(str(row[6])) if len(row) > 6 else Decimal("0"),
            )
            found[open_time] = bar
            oldest = open_time if oldest is None else min(oldest, open_time)
        if oldest is None or oldest <= start_ms or len(rows) < 100:
            break
        after = oldest
    return list(found.values())


def _bar_from_binance_row(row: list) -> Bar:
    return Bar(
        open_time=_epoch_ms(row[0]),
        open=Decimal(str(row[1])),
        high=Decimal(str(row[2])),
        low=Decimal(str(row[3])),
        close=Decimal(str(row[4])),
        volume=Decimal(str(row[5])),
        quote_volume=Decimal(str(row[7])) if len(row) > 7 else Decimal("0"),
        trades=int(row[8]) if len(row) > 8 and str(row[8]) not in {"", "None"} else 0,
        taker_buy_base=Decimal(str(row[9])) if len(row) > 9 else Decimal("0"),
    )


def _epoch_ms(value: object) -> int:
    number = int(str(value).split(".", 1)[0])
    if number >= 10**15:
        return number // 1000
    if number >= 10**12:
        return number
    return number * 1000


def _months(start_ms: int, end_ms: int):
    start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        month_start = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000)
        if month == 12:
            nxt = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            nxt = datetime(year, month + 1, 1, tzinfo=timezone.utc)
        month_end = int(nxt.timestamp() * 1000) - 1
        yield year, month, month_start, month_end
        month += 1
        if month == 13:
            month = 1
            year += 1


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _okx_inst(symbol: str) -> str:
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[: -len(quote)]}-{quote}"
    raise ValueError(f"无法把 {symbol} 转成 OKX 现货名")
