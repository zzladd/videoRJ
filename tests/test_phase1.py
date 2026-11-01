"""第一期影子盘的离线验收。不连接交易所。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import pandas as pd

from nautilus_trader.adapters.binance.config import BinanceDataClientConfig
from nautilus_trader.adapters.binance.config import BinanceExecClientConfig
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.engine import BacktestEngineConfig
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.backtest.models import MakerTakerFeeModel
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.model.currencies import BTC
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.test_kit.stubs.data import TestDataStubs

from paper_spot import runtime
from paper_spot.config_loader import ConfigError
from paper_spot.config_loader import canonical_hash
from paper_spot.config_loader import load_domestic
from paper_spot.config_loader import load_fee
from paper_spot.config_loader import repo_root
from paper_spot.fees import stamp_currency_pair
from paper_spot.gap import GapGate
from paper_spot.guard import ShadowGuardError
from paper_spot.guard import assert_data_config
from paper_spot.guard import assert_exec_config
from paper_spot.guard import assert_execution_client
from paper_spot.guard import assert_no_trading_secrets
from paper_spot.ledger import LedgerError
from paper_spot.ledger import ShadowLedger
from paper_spot.node_builder import build_node
from paper_spot.replay import diff_count
from paper_spot.replay import fill_keys
from paper_spot.replay import replay_diff
from paper_spot.reports import acceptance
from paper_spot.reports import data_quality
from paper_spot.reports import quote_frame
from paper_spot.reports import write_daily_summary
from paper_spot.reports import write_quantstats
from paper_spot.runtime import Session
from paper_spot.sandbox_client import PaperSandboxExecutionClient
from paper_spot.strategy import ConnectivityConfig
from paper_spot.venue_probe import ProbeStatus
from paper_spot.venue_probe import choose_venue


def _config(**overrides) -> ConnectivityConfig:
    values = {
        "instrument_id": "BTCUSDT.BINANCE",
        "order_quote_notional": "12",
        "max_notional": "50",
        "imbalance_multiple": "1.2",
        "protection_band": "0.005",
        "cooldown_seconds": 5.0,
        "max_orders": 6,
        "session_seconds": 0,
        "use_exchange_depth_params": False,
        "maker_fee": "0.001",
        "taker_fee": "0.001",
        "fee_version": "binance-spot-regular-2026-11-01",
        "ledger_path": "",
        "starting_balance": "10000 USDT",
    }
    values.update(overrides)
    return ConnectivityConfig(**values)


def _run(instrument, data, config, balances=None):
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="PAPER-001",
            logging=LoggingConfig(log_level="ERROR"),
        )
    )
    try:
        engine.add_venue(
            venue=instrument.id.venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            starting_balances=[Money.from_str(item) for item in (balances or ["10000 USDT"])],
            base_currency=None,
            fill_model=FillModel(),
            fee_model=MakerTakerFeeModel(),
            latency_model=__import__(
                "paper_spot.sandbox_client", fromlist=["static_latency_model"]
            ).static_latency_model(config.latency_ns),
            book_type=BookType.L2_MBP,
            trade_execution=True,
            liquidity_consumption=True,
            bar_execution=False,
            use_message_queue=True,
        )
        engine.add_instrument(instrument)
        engine.add_data(data)
        from paper_spot.strategy import ConnectivityStrategy

        engine.add_strategy(ConnectivityStrategy(config))
        engine.run()
        account = engine.portfolio.account(instrument.id.venue)
        return {
            "fills": fill_keys(engine),
            "usdt": account.balance_total(USDT).as_decimal(),
            "btc": account.balance_total(BTC).as_decimal() if account.balance_total(BTC) else Decimal(0),
            "orders": [
                (order.status.name, str(order.filled_qty), str(order.quantity))
                for order in engine.cache.orders()
            ],
        }
    finally:
        engine.dispose()


class Phase1Tests(unittest.TestCase):
    def tearDown(self) -> None:
        runtime.CURRENT = None

    def test_official_fee_file_and_empty_domestic_master(self) -> None:
        root = repo_root()
        fee = load_fee(root, "BINANCE")
        self.assertEqual(fee["fee_version"], "binance-spot-regular-2026-11-01")
        self.assertEqual(fee["account_tier"], "Regular User")
        self.assertEqual(Decimal(fee["maker_rate"]), Decimal("0.001"))
        self.assertEqual(Decimal(fee["taker_rate"]), Decimal("0.001"))
        self.assertEqual(load_domestic(root)["instruments"], [])

    def test_fee_file_rejects_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config" / "fees"
            path.mkdir(parents=True)
            (path / "binance_spot_regular.json").write_text(
                json.dumps(
                    {
                        "fee_version": "bad",
                        "venue": "BINANCE",
                        "account_tier": "Regular User",
                        "maker_rate": "0",
                        "taker_rate": "0.001",
                        "retrieved_on": "2026-11-01",
                        "source": "https://www.binance.com/en/fee/trading",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_fee(root, "BINANCE")

    def test_stamp_replaces_zero_fees(self) -> None:
        instrument = TestInstrumentProvider.btcusdt_binance()
        stamped = stamp_currency_pair(instrument, Decimal("0.001"), Decimal("0.001"))
        self.assertEqual(stamped.maker_fee, Decimal("0.001"))
        self.assertEqual(stamped.taker_fee, Decimal("0.001"))
        with self.assertRaises(ValueError):
            stamp_currency_pair(instrument, Decimal(0), Decimal("0.001"))

    def test_choose_venue_uses_binance_market_hosts_then_bybit(self) -> None:
        ok = ProbeStatus(True, "HTTP 200")
        blocked = ProbeStatus(False, "HTTP 451")
        choice = choose_venue(
            {
                "binance_primary_http": blocked,
                "binance_primary_ws": blocked,
                "binance_market_http": ok,
                "binance_market_ws": ok,
                "bybit_http": ok,
                "bybit_ws": ok,
                "okx_http": ok,
                "okx_ws": ok,
            }
        )
        self.assertEqual(choice.venue, "BINANCE")
        self.assertEqual(choice.instrument_id, "BTCUSDT.BINANCE")
        self.assertEqual(choice.http_base, "https://data-api.binance.vision")
        self.assertEqual(choice.ws_base, "wss://data-stream.binance.vision")
        bybit = choose_venue(
            {
                "binance_primary_http": blocked,
                "binance_primary_ws": blocked,
                "binance_market_http": blocked,
                "binance_market_ws": blocked,
                "bybit_http": ok,
                "bybit_ws": ok,
                "okx_http": ok,
                "okx_ws": ok,
            }
        )
        self.assertEqual(bybit.venue, "BYBIT")
        self.assertEqual(bybit.instrument_id, "BTCUSDT-SPOT.BYBIT")
        with self.assertRaises(ConfigError):
            choose_venue({})

    def test_guard_rejects_exchange_exec_secrets_proxy_and_foreign_host(self) -> None:
        with self.assertRaises(ShadowGuardError):
            assert_exec_config(BinanceExecClientConfig())
        os.environ["BINANCE_API_KEY"] = "not-printed"
        try:
            with self.assertRaises(ShadowGuardError) as caught:
                assert_no_trading_secrets()
            self.assertNotIn("not-printed", str(caught.exception))
        finally:
            del os.environ["BINANCE_API_KEY"]
        with self.assertRaises(ShadowGuardError):
            assert_data_config(BinanceDataClientConfig(proxy_url="http://127.0.0.1:9"))
        with self.assertRaises(ConfigError):
            assert_data_config(BinanceDataClientConfig(base_url_http="https://example.com/api"))
        with self.assertRaises(ConfigError):
            assert_data_config(BinanceDataClientConfig(base_url_http="https://user:secret@api.binance.com/api"))

        class BinanceExecutionClient:
            pass

        BinanceExecutionClient.__module__ = "nautilus_trader.adapters.binance.execution"
        with self.assertRaises(ShadowGuardError):
            assert_execution_client(BinanceExecutionClient())

    def test_sandbox_client_has_100ms_queue_and_fee_model(self) -> None:
        loop = asyncio.new_event_loop()
        clock = LiveClock()
        msgbus = MessageBus(trader_id=TraderId("PAPER-001"), clock=clock)
        cache = Cache()
        portfolio = Portfolio(msgbus, cache, clock)
        client = PaperSandboxExecutionClient(
            loop=loop,
            portfolio=portfolio,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=SandboxExecutionClientConfig(
                venue="BINANCE",
                starting_balances=["10000 USDT"],
                account_type="CASH",
                oms_type="NETTING",
                book_type="L2_MBP",
                trade_execution=True,
                bar_execution=False,
            ),
        )
        assert_execution_client(client)
        loop.close()

    def test_ledger_rejects_duplicate_fill_and_restores_balance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.json"
            ledger = ShadowLedger(path, ["10000 USDT"])
            fill = {"trade_id": "T1", "side": "BUY"}
            ledger.apply_fill(fill, {"USDT": "9988", "BTC": "0.00015"})
            with self.assertRaises(LedgerError):
                ledger.apply_fill(fill, {"USDT": "9988", "BTC": "0.00015"})
            restored = ShadowLedger(path, ["10000 USDT"]).balances_for_start(["10000 USDT"])
            self.assertIn("9988 USDT", restored)
            self.assertIn("0.00015 BTC", restored)

    def test_gap_gate_drops_stale_and_explicit_window(self) -> None:
        gate = GapGate(stale_ns=2_000_000_000)
        self.assertTrue(gate.reject(1_000, 3_000_000_000))
        self.assertFalse(gate.blocks(1_000_000_000, 1_100_000_000))
        gate.add_gap(10, 20)
        self.assertTrue(gate.blocks(15, 15))
        self.assertFalse(gate.blocks(21, 21))

    def test_hash_is_stable(self) -> None:
        payload = {"b": 1, "a": "通路测试"}
        self.assertEqual(canonical_hash(payload), canonical_hash({"a": "通路测试", "b": 1}))

    def test_latency_blocks_fill_before_100ms(self) -> None:
        instrument = TestInstrumentProvider.btcusdt_binance()
        book = TestDataStubs.order_book_snapshot(
            instrument=instrument,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=5,
            ask_size=1,
            bid_levels=1,
            ask_levels=1,
        )
        quote = TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=5,
            ask_size=1,
            ts_event=0,
            ts_init=0,
        )
        later = TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=5,
            ask_size=1,
            ts_event=50_000_000,
            ts_init=50_000_000,
        )
        early = _run(instrument, [book, quote, later], _config())
        self.assertEqual(early["fills"], [], early["orders"])
        released = TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=5,
            ask_size=1,
            ts_event=150_000_000,
            ts_init=150_000_000,
        )
        filled = _run(instrument, [book, quote, released], _config())
        self.assertEqual(len(filled["fills"]), 1, filled["orders"])
        self.assertEqual(filled["fills"][0][0], "BUY")
        self.assertLess(filled["usdt"], Decimal("10000") - Decimal("100.01") * Decimal(filled["fills"][0][1]))

    def test_ioc_walks_inside_band_and_cancels_the_rest(self) -> None:
        instrument = TestInstrumentProvider.btcusdt_binance()
        book = TestDataStubs.order_book_snapshot(
            instrument=instrument,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=5,
            ask_size=0.001,
            bid_levels=1,
            ask_levels=3,
        )
        quote = TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=5,
            ask_size=0.001,
        )
        released = TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=5,
            ask_size=0.001,
            ts_event=150_000_000,
            ts_init=150_000_000,
        )
        result = _run(instrument, [book, quote, released], _config())
        self.assertEqual(result["fills"], [("BUY", "0.001000", "100.01")], result)
        self.assertEqual(result["orders"][0][0], "CANCELED")
        self.assertGreater(Decimal(result["orders"][0][2]), Decimal(result["orders"][0][1]))
        notional = Decimal("0.001") * Decimal("100.01")
        self.assertLess(result["usdt"], Decimal("10000") - notional)
        self.assertGreater(result["usdt"], Decimal("10000") - notional - Decimal("0.01"))

    def test_cash_account_can_sell_base_balance(self) -> None:
        instrument = TestInstrumentProvider.btcusdt_binance()
        book = TestDataStubs.order_book_snapshot(
            instrument=instrument,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=1,
            ask_size=5,
            bid_levels=1,
            ask_levels=1,
        )
        quote = TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=1,
            ask_size=5,
        )
        released = TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=1,
            ask_size=5,
            ts_event=150_000_000,
            ts_init=150_000_000,
        )
        result = _run(
            instrument,
            [book, quote, released],
            _config(),
            balances=["10000 USDT", "0.150000 BTC"],
        )
        self.assertTrue(result["fills"], result["orders"])
        self.assertEqual(result["fills"][0][0], "SELL", result)
        self.assertLess(result["btc"], Decimal("0.15"))
        self.assertGreater(result["usdt"], Decimal("10000"))

    def test_replay_of_identical_runs_is_zero(self) -> None:
        instrument = TestInstrumentProvider.btcusdt_binance()
        book = TestDataStubs.order_book_snapshot(
            instrument=instrument,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=5,
            ask_size=1,
            bid_levels=1,
            ask_levels=1,
        )
        quote = TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=5,
            ask_size=1,
        )
        released = TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=5,
            ask_size=1,
            ts_event=150_000_000,
            ts_init=150_000_000,
        )
        data = [book, quote, released]
        self.assertEqual(replay_diff(instrument, data, _config()), 0)
        self.assertEqual(diff_count([("BUY", "1", "2")], [("BUY", "1", "2")]), 0)
        self.assertEqual(diff_count([("BUY", "1", "2")], [("BUY", "1", "3")]), 1)

    def test_catalog_round_trip_and_report_header_fields(self) -> None:
        instrument = TestInstrumentProvider.btcusdt_binance()
        quote = TestDataStubs.quote_tick(
            instrument=instrument,
            bid_price=100.00,
            ask_price=100.01,
            bid_size=1,
            ask_size=2,
            ts_event=1_000,
            ts_init=1_000,
        )
        with tempfile.TemporaryDirectory() as temporary:
            catalog = ParquetDataCatalog(temporary)
            catalog.write_data([quote, instrument], skip_disjoint_check=True)
            loaded = catalog.query(QuoteTick)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(str(loaded[0].bid_price), "100.00")
            frame = quote_frame(loaded)
            quality = data_quality(frame, pd.DataFrame())
            self.assertEqual(quality["time_inversions"], 0)
            crossed = quote_frame(
                [
                    TestDataStubs.quote_tick(
                        instrument=instrument,
                        bid_price=100.02,
                        ask_price=100.01,
                        ts_event=1,
                        ts_init=1,
                    )
                ]
            )
            bad = data_quality(crossed, pd.DataFrame())
            self.assertGreater(bad["crossed_books"], 0)
            self.assertFalse(bad["passed"])
            daily = write_daily_summary(
                Path(temporary) / "daily_returns.txt",
                pd.Series(dtype="float64"),
                pd.DataFrame(),
                Decimal("10000"),
            )
            note = write_quantstats(daily, Path(temporary) / "quantstats.html")
            self.assertIsNotNone(note)
            self.assertIn("日收益", Path(temporary, "daily_returns.txt").read_text(encoding="utf-8"))
            index = pd.to_datetime(["2026-11-01", "2026-11-02", "2026-11-03"], utc=True)
            series = pd.Series([0.001, -0.002, 0.0004], index=index)
            html_path = Path(temporary) / "quantstats.html"
            self.assertIsNone(write_quantstats(series, html_path))
            self.assertTrue(html_path.is_file())
            self.assertGreater(html_path.stat().st_size, 1000)
            verdict = acceptance(
                quality=quality,
                fill_book=pd.DataFrame(),
                participation_rate=None,
                replay_diff=0,
                session_seconds=120,
            )
            self.assertTrue(verdict["report_header"]["met"])
            self.assertTrue(verdict["no_exchange_execution_client"]["met"])
            self.assertFalse(verdict["continuous_24h_and_resume"]["met"])
            self.assertFalse(verdict["disconnect_does_not_fill_old_orders"]["met"])
            self.assertTrue(verdict["disconnect_does_not_fill_old_orders"]["mechanism_tested"])
            self.assertTrue(verdict["replay_diff_zero"]["met"])
            self.assertFalse(verdict["participation_fee_arrival"]["met"])

    def test_built_node_registers_only_the_sandbox(self) -> None:
        root = repo_root()
        fee = load_fee(root, "BINANCE")
        _, paper = __import__("paper_spot.config_loader", fromlist=["load_paper"]).load_paper(root)
        runtime.CURRENT = Session(
            fee_maker=Decimal(fee["maker_rate"]),
            fee_taker=Decimal(fee["taker_rate"]),
            fee_version=fee["fee_version"],
            gate=GapGate(),
            instrument_id="BTCUSDT.BINANCE",
        )
        from paper_spot.venue_probe import VenueChoice

        choice = VenueChoice(
            venue="BINANCE",
            instrument_id="BTCUSDT.BINANCE",
            http_base="https://data-api.binance.vision",
            ws_base="wss://data-stream.binance.vision",
            reason="单元测试不连接行情",
        )
        with tempfile.TemporaryDirectory() as temporary:
            node = build_node(
                run_dir=Path(temporary),
                paper=paper,
                fee=fee,
                choice=choice,
                starting_balances=["10000 USDT"],
                session_seconds=0,
                imbalance="1.2",
                log_level="ERROR",
            )
            try:
                clients = list(node.kernel.exec_engine._clients.values())
                self.assertEqual([type(client).__name__ for client in clients], ["PaperSandboxExecutionClient"])
                self.assertEqual(list(node._builder._exec_factories), ["BINANCE"])
                self.assertIn("Sandbox", node._builder._exec_factories["BINANCE"].__name__)
                self.assertNotIn("BinanceLiveExecClientFactory", "".join(node._builder._exec_factories))
                data_config = node.kernel._config.data_clients["BINANCE"]
                self.assertIsNone(data_config.api_key)
                self.assertFalse(getattr(data_config, "proxy_url", None))
                self.assertFalse(node.kernel._config.exec_engine.reconciliation)
            finally:
                node.dispose()


if __name__ == "__main__":
    unittest.main()
