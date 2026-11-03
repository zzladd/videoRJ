"""本地 K 线、双均线和研页接口。"""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.request import Request
from urllib.request import urlopen

from paper_spot.config_loader import ConfigError
from paper_spot.config_loader import assert_official_url
from paper_spot.research.fetch import HttpBody
from paper_spot.research.fetch import fetch_closed_bars
from paper_spot.research.fetch import parse_binance_csv
from paper_spot.research.indicators import macd
from paper_spot.research.indicators import rsi
from paper_spot.research.indicators import sma
from paper_spot.research.service import catch_up
from paper_spot.research.service import chart_payload
from paper_spot.research.service import scan_dual_ma
from paper_spot.research.service import sync_range
from paper_spot.research.service import validate_dual_ma
from paper_spot.research.store import Bar
from paper_spot.research.store import load_bars
from paper_spot.research.store import read_coverage
from paper_spot.research.store import upsert_bars
from paper_spot.research.strategy import dual_ma_grid
from paper_spot.research.strategy import simulate_dual_ma
from paper_spot.site import Site
from paper_spot.site import bind_site

DAY = 86_400_000
NOW = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _bar(day: int, close: int, open_price: int | None = None) -> Bar:
    opened = close if open_price is None else open_price
    return Bar(
        open_time=day * DAY,
        open=Decimal(opened),
        high=Decimal(max(opened, close) + 1),
        low=Decimal(min(opened, close) - 1),
        close=Decimal(close),
        volume=Decimal("2"),
        quote_volume=Decimal("20"),
        trades=4,
        taker_buy_base=Decimal("1"),
    )


def _fee(root: Path) -> None:
    path = root / "config" / "fees" / "binance_spot_regular.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "fee_version": "test-fee",
                "venue": "BINANCE",
                "product": "spot",
                "account_tier": "Regular User",
                "maker_rate": "0.001",
                "taker_rate": "0.001",
                "retrieved_on": "2026-11-01",
                "source": "https://www.binance.com/en/fee/trading",
            }
        ),
        encoding="utf-8",
    )


def _daily(start: datetime, days: int, price: int = 100) -> list[Bar]:
    bars = []
    for offset in range(days):
        moment = start + timedelta(days=offset)
        bars.append(
            Bar(
                open_time=int(moment.timestamp() * 1000),
                open=Decimal(price),
                high=Decimal(price + 1),
                low=Decimal(price - 1),
                close=Decimal(price),
                volume=Decimal("3"),
            )
        )
    return bars


class ResearchTests(unittest.TestCase):
    def test_indicators_match_the_chosen_definitions(self) -> None:
        self.assertEqual(sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])
        rising = [float(value) for value in range(1, 21)]
        self.assertEqual(rsi(rising)[14], 100.0)
        lines = macd([float(value) for value in range(1, 80)])
        for dif, dea, hist in zip(lines["dif"], lines["dea"], lines["hist"]):
            if dif is None or dea is None:
                self.assertIsNone(hist)
            else:
                self.assertAlmostEqual(hist, 2 * (dif - dea))

    def test_dual_ma_fills_next_open_and_charges_taker_fee(self) -> None:
        closes = [10, 10, 10, 10, 12, 14, 16, 14, 12, 8]
        bars = [_bar(index, close) for index, close in enumerate(closes)]
        bars[5] = _bar(5, 14, 20)
        bars[9] = _bar(9, 8, 8)
        result = simulate_dual_ma(bars, 2, 3, Decimal("0.001"))
        self.assertEqual(result["trades"], 2)
        self.assertEqual(result["fills"][0]["side"], "BUY")
        self.assertEqual(Decimal(result["fills"][0]["price"]), Decimal("20"))
        self.assertEqual(Decimal(result["fills"][0]["fee"]), Decimal("10"))
        self.assertEqual(result["fills"][1]["side"], "SELL")
        self.assertEqual(Decimal(result["fills"][1]["price"]), Decimal("8"))
        self.assertEqual(Decimal(result["fills"][1]["fee"]), Decimal("3.996"))
        self.assertEqual(Decimal(result["fills"][1]["pnl"]), Decimal("-6007.996"))
        self.assertEqual(Decimal(result["ending_equity"]), Decimal("3992.004"))
        self.assertEqual([signal["side"] for signal in result["signals"]], ["BUY", "SELL"])
        self.assertEqual(len(dual_ma_grid()), 9)
        self.assertTrue(all(fast < slow for fast, slow in dual_ma_grid()))

    def test_store_round_trip_and_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bars = [_bar(0, 10), _bar(1, 11), _bar(3, 12)]
            coverage = upsert_bars(root, "BINANCE", "BTCUSDT", "1d", bars)
            loaded = load_bars(root, "BINANCE", "BTCUSDT", "1d")
            self.assertEqual([bar.open_time for bar in loaded], [0, DAY, 3 * DAY])
            self.assertEqual(loaded[0].taker_buy_base, Decimal("1"))
            self.assertEqual(coverage["gaps"], [[2 * DAY, 3 * DAY]])
            self.assertEqual(read_coverage(root, "BINANCE", "BTCUSDT", "1d")["count"], 3)
            with self.assertRaises(ValueError):
                upsert_bars(root, "binance", "BTCUSDT", "1d", bars)

    def test_binance_zip_falls_back_to_rest_and_drops_open_bar(self) -> None:
        january = (
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_base,taker_buy_quote,ignore\n"
            "1704067200000000,100,110,90,105,12,1704153599999999,1200,3,4,400,0\n"
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("BTCUSDT-1d-2024-01.csv", january)
        blob = buffer.getvalue()
        seen: list[str] = []

        def opener(url: str) -> HttpBody:
            seen.append(url)
            if url.startswith("https://data.binance.vision/") and "2024-01" in url:
                return HttpBody(200, blob)
            if url.startswith("https://data.binance.vision/"):
                return HttpBody(404, b"")
            row = [
                1706745600000,
                "200",
                "210",
                "190",
                "205",
                "8",
                1706831999999,
                "1600",
                2,
                "3",
                "600",
                "0",
            ]
            return HttpBody(200, json.dumps([row]).encode("utf-8"))

        now_ms = 1706745600000 + 1000
        bars = fetch_closed_bars(
            "BINANCE",
            "BTCUSDT",
            "1d",
            1704067200000,
            1706831999999,
            now_ms,
            opener,
        )
        self.assertEqual([bar.open_time for bar in bars], [1704067200000])
        self.assertEqual(bars[0].close, Decimal("105"))
        self.assertEqual(bars[0].trades, 3)
        self.assertTrue(any("data-api.binance.vision" in url for url in seen))
        self.assertEqual(parse_binance_csv("1704067200000,1,2,0.5,1.5,9\n")[0].open, Decimal("1"))
        with self.assertRaises(ConfigError):
            assert_official_url("https://example.com/klines")
        assert_official_url("https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1d/a.zip")

    def test_scan_blocks_gaps_and_validation_is_out_of_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _fee(root)
            start = datetime(2024, 1, 1, tzinfo=timezone.utc)
            upsert_bars(root, "BINANCE", "BTCUSDT", "1d", _daily(start, 366))
            ranked = scan_dual_ma(root, "BINANCE", "BTCUSDT", "1d", "2024-01-01", "2024-06-30", NOW)
            self.assertEqual(len(ranked["rows"]), 9)
            self.assertGreaterEqual(Decimal(ranked["rows"][0]["return_rate"]), Decimal(ranked["rows"][-1]["return_rate"]))
            with self.assertRaisesRegex(ValueError, "不同"):
                validate_dual_ma(
                    root,
                    "BINANCE",
                    "BTCUSDT",
                    "1d",
                    10,
                    30,
                    "2024-01-01",
                    "2024-06-30",
                    "2024-01-01",
                    "2024-06-30",
                    NOW,
                )
            with self.assertRaisesRegex(ValueError, "重叠"):
                validate_dual_ma(
                    root,
                    "BINANCE",
                    "BTCUSDT",
                    "1d",
                    10,
                    30,
                    "2024-01-01",
                    "2024-06-30",
                    "2024-06-01",
                    "2024-12-31",
                    NOW,
                )
            saved = validate_dual_ma(
                root,
                "BINANCE",
                "BTCUSDT",
                "1d",
                10,
                30,
                "2024-01-01",
                "2024-06-30",
                "2024-07-01",
                "2024-12-31",
                NOW,
            )
            path = root / "data" / "experiments" / saved["id"] / "result.json"
            self.assertTrue(path.is_file())
            self.assertIn("不是 L2 沙箱", saved["note"])
            chart = chart_payload(
                root,
                "BINANCE",
                "BTCUSDT",
                "1d",
                10,
                30,
                "2024-07-01",
                "2024-12-31",
                NOW,
            )
            self.assertGreater(chart["count"], 100)
            self.assertEqual(chart["candles"][0]["open"], 100.0)
            self.assertIn("hist", chart["macd"])
            gappy = [_bar(10, 10), _bar(12, 11)]
            upsert_bars(root, "BINANCE", "BTCUSDT", "1h", gappy)
            with self.assertRaisesRegex(ValueError, "缺口"):
                scan_dual_ma(root, "BINANCE", "BTCUSDT", "1h", "1970-01-01", "1970-01-01", NOW)

    def test_sync_writes_closed_bars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def opener(url: str) -> HttpBody:
                if "data.binance.vision" in url:
                    return HttpBody(404, b"")
                row = [1704067200000, "1", "2", "1", "2", "3", 1704153599999, "4", 1, "1", "1", "0"]
                return HttpBody(200, json.dumps([row]).encode("utf-8"))

            result = sync_range(
                root,
                "BINANCE",
                "BTCUSDT",
                "1d",
                1704067200000,
                1704153599999,
                opener,
                NOW,
            )
            self.assertEqual(result["fetched"], 1)
            self.assertEqual(result["coverage"]["count"], 1)

    def test_catch_up_keeps_history_and_appends_the_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls: list[str] = []

            def opener(url: str) -> HttpBody:
                calls.append(url)
                if "data.binance.vision" in url:
                    return HttpBody(404, b"")
                row = [1704153600000, "3", "4", "2", "3", "5", 1704239999999, "6", 1, "1", "1", "0"]
                return HttpBody(200, json.dumps([row]).encode("utf-8"))

            empty = catch_up(root, "BINANCE", "BTCUSDT", "1d", opener, NOW)
            self.assertEqual(empty["fetched"], 0)
            self.assertEqual(calls, [])
            upsert_bars(
                root,
                "BINANCE",
                "BTCUSDT",
                "1d",
                [
                    Bar(
                        open_time=1704067200000,
                        open=Decimal("1"),
                        high=Decimal("2"),
                        low=Decimal("1"),
                        close=Decimal("2"),
                        volume=Decimal("3"),
                    )
                ],
            )
            current = catch_up(root, "BINANCE", "BTCUSDT", "1d", opener, 1704153601000)
            self.assertEqual(current["fetched"], 0)
            self.assertEqual(calls, [])
            appended = catch_up(root, "BINANCE", "BTCUSDT", "1d", opener, NOW)
            self.assertEqual(appended["fetched"], 1)
            self.assertEqual(appended["coverage"]["count"], 2)
            self.assertTrue(all("startTime=1704153600000" in url or "data.binance.vision" in url for url in calls))
            self.assertFalse(any("startTime=1704067200000" in url for url in calls))

    def test_research_page_and_json_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            site = Site(root=Path(temporary), runs_dir=Path(temporary) / "runs")
            server = bind_site(site, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                page = urlopen(f"http://127.0.0.1:{port}/research", timeout=5).read().decode("utf-8")
                self.assertIn("样本外验证", page)
                self.assertIn("不是 L2 沙箱", page)
                script = urlopen(f"http://127.0.0.1:{port}/research/app.js", timeout=5).read().decode("utf-8")
                self.assertIn("localStorage", script)
                vendor = urlopen(
                    f"http://127.0.0.1:{port}/research/lightweight-charts.standalone.production.js",
                    timeout=5,
                ).read()
                self.assertIn(b"Lightweight Charts", vendor)
                chart = json.loads(
                    urlopen(
                        f"http://127.0.0.1:{port}/api/research/chart?interval=1d&fast=10&slow=30",
                        timeout=5,
                    ).read()
                )
                self.assertEqual(chart["candles"], [])
                body = json.dumps(
                    {
                        "venue": "BINANCE",
                        "symbol": "BTCUSDT",
                        "interval": "1d",
                        "start": "2024-01-01",
                        "end": "2024-01-10",
                    }
                ).encode()
                request = Request(
                    f"http://127.0.0.1:{port}/api/research/scan",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(Exception) as caught:
                    urlopen(request, timeout=5)
                self.assertEqual(caught.exception.code, 400)
                payload = json.loads(caught.exception.read().decode("utf-8"))
                self.assertIn("缺口", payload["error"])
                caught_up = json.loads(
                    urlopen(
                        Request(
                            f"http://127.0.0.1:{port}/api/research/catchup",
                            data=body,
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        ),
                        timeout=5,
                    ).read()
                )
                self.assertEqual(caught_up["fetched"], 0)
                self.assertFalse(caught_up["appended"])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
