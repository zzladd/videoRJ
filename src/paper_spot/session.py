"""一次通路测试的准备、运行和报告。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import nautilus_trader
from nautilus_trader.model.data import OrderBookDeltas
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

from paper_spot.config_loader import ConfigError
from paper_spot.config_loader import canonical_hash
from paper_spot.config_loader import load_domestic
from paper_spot.config_loader import load_fee
from paper_spot.config_loader import load_instrument_master
from paper_spot.config_loader import load_paper
from paper_spot.gap import GapGate
from paper_spot.guard import assert_no_trading_secrets
from paper_spot.ledger import ShadowLedger
from paper_spot.node_builder import build_node
from paper_spot.replay import _data_sort_key
from paper_spot.replay import diff_count
from paper_spot.replay import run_backtest
from paper_spot.reports import acceptance
from paper_spot.reports import build_fill_book
from paper_spot.reports import data_quality
from paper_spot.reports import equity_curve
from paper_spot.reports import participation
from paper_spot.reports import quote_frame
from paper_spot.reports import trade_frame
from paper_spot.reports import write_daily_summary
from paper_spot.reports import write_fill_book
from paper_spot.reports import write_quantstats
from paper_spot import runtime
from paper_spot.runtime import Session
from paper_spot.strategy import ConnectivityConfig
from paper_spot.venue_probe import VenueChoice
from paper_spot.venue_probe import fetch_binance_filters
from paper_spot.venue_probe import probe_venues


def matching_payload(paper: dict[str, Any], fee: dict[str, Any], choice: VenueChoice, imbalance: str) -> dict[str, Any]:
    return {
        "phase": 1,
        "title": "通路测试",
        "venue": choice.venue,
        "instrument_id": choice.instrument_id,
        "http_base": choice.http_base,
        "ws_base": choice.ws_base,
        "fee_version": fee["fee_version"],
        "account_tier": fee["account_tier"],
        "maker_rate": fee["maker_rate"],
        "taker_rate": fee["taker_rate"],
        "retrieved_on": fee["retrieved_on"],
        "latency_ns": 100_000_000,
        "latency_model": "LatencyModel",
        "book_type": "L2_MBP",
        "book_depth": int(paper["book_depth"]),
        "trade_execution": True,
        "liquidity_consumption": True,
        "bar_execution": False,
        "protection_band": str(paper["protection_band"]),
        "order_quote_notional": str(paper["order_quote_notional"]),
        "max_notional_per_order": str(paper["max_notional_per_order"]),
        "imbalance_multiple": str(imbalance),
        "starting_balance": str(paper["starting_balance"]),
        "account_type": "CASH",
        "oms_type": "NETTING",
    }


def load_phase1(root: Path) -> dict[str, Any]:
    assert_no_trading_secrets()
    load_domestic(root)
    _, paper = load_paper(root)
    if paper.get("phase") != 1 or paper.get("product") != "spot":
        raise ConfigError("这个程序只跑第一期现货影子盘")
    if int(paper["latency_ms"]) != 100:
        raise ConfigError("静态延迟必须是 100 毫秒")
    if paper.get("account_type") != "CASH":
        raise ConfigError("第一期账户必须是现金账户")
    return paper


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def prepare_run(
    root: Path,
    run_dir: Path,
    *,
    resume: bool,
    seconds: int | None,
    imbalance: str | None,
) -> dict[str, Any]:
    paper = load_phase1(root)
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = run_dir / "ledger.json"
    snapshot_path = run_dir / "config_snapshot.json"
    if ledger_path.is_file() and not resume:
        raise ConfigError(f"{ledger_path} 已有影子账本。继续这一次请加 --resume，以免把现金重置成 10000。")
    if resume and not ledger_path.is_file():
        raise ConfigError(f"{run_dir} 没有可恢复的影子账本")
    if resume:
        if not snapshot_path.is_file():
            raise ConfigError("恢复时缺少 config_snapshot.json，不能核对配置哈希")
        saved = _read_json(snapshot_path)
        choice = VenueChoice(**saved["venue"])
        saved_imbalance = str(saved["matching"]["imbalance_multiple"])
        if imbalance is not None and str(imbalance) != saved_imbalance:
            raise ConfigError("恢复时不能改不平衡倍数")
        imbalance = saved_imbalance
        probe_details = saved.get("probe") or {}
    else:
        choice, probe_details = probe_venues()
        imbalance = str(imbalance if imbalance is not None else paper["imbalance_multiple"])
    fee = load_fee(root, choice.venue)
    payload = matching_payload(paper, fee, choice, imbalance)
    config_hash = canonical_hash(payload)
    if resume:
        saved_hash = _read_json(snapshot_path)["hash"]
        if saved_hash != config_hash:
            raise ConfigError("配置哈希和上次不一致，拒绝在这个目录上恢复")
    else:
        _write_json(
            snapshot_path,
            {
                "title": "通路测试",
                "hash": config_hash,
                "matching": payload,
                "fee": fee,
                "venue": choice.as_dict(),
                "probe": probe_details,
                "nautilus": nautilus_trader.__version__,
            },
        )
    session_seconds = int(paper["session_seconds"] if seconds is None else seconds)
    if session_seconds < 0:
        raise ConfigError("会话时长不能是负数")
    note = filter_note(root, choice)
    snapshot = _read_json(snapshot_path)
    snapshot["filter_note"] = note
    _write_json(snapshot_path, snapshot)
    balances = [str(paper["starting_balance"])]
    if resume:
        balances = ShadowLedger(ledger_path, balances).balances_for_start(balances)
    gate = GapGate(path=run_dir / "gaps.json")
    runtime.CURRENT = Session(
        fee_maker=Decimal(fee["maker_rate"]),
        fee_taker=Decimal(fee["taker_rate"]),
        fee_version=str(fee["fee_version"]),
        gate=gate,
        instrument_id=choice.instrument_id,
    )
    meta_path = run_dir / "run_meta.json"
    meta = _read_json(meta_path) if meta_path.is_file() else {"attempts": []}
    meta["attempts"].append(
        {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "resume": resume,
            "session_seconds": session_seconds,
            "starting_balances": balances,
        }
    )
    _write_json(meta_path, meta)
    return {
        "paper": paper,
        "fee": fee,
        "choice": choice,
        "probe": probe_details,
        "hash": config_hash,
        "imbalance": imbalance,
        "seconds": session_seconds,
        "balances": balances,
        "filter_note": note,
    }


def filter_note(root: Path, choice: VenueChoice) -> str:
    if choice.venue != "BINANCE":
        return "场所不是 Binance。下单精度以当次官方 instrument 响应为准。"
    base = choice.http_base or "https://api.binance.com"
    live = fetch_binance_filters(base)
    if not live:
        return "当次没有取到 exchangeInfo。下单精度仍以适配器载入的品种为准。"
    master = load_instrument_master(root)
    rows = [item for item in master.get("instruments", []) if item.get("instrument_id") == choice.instrument_id]
    if not rows:
        return "品种主数据里没有这个交易对。下单精度以当次官方响应为准。"
    expected = rows[0]
    mismatched = []
    for field in ("price_increment", "size_increment", "min_quantity", "min_notional"):
        left = _decimal_text(expected.get(field, ""))
        right = _decimal_text(live.get(field, ""))
        if left and right and left != right:
            mismatched.append(f"{field} 主数据 {left}，当次 {right}")
    if live.get("status") and live["status"] != "TRADING":
        mismatched.append(f"状态 {live['status']}")
    if mismatched:
        return "规格和 config/instruments/crypto_spot.json 不一致：" + "；".join(mismatched)
    return "规格和 config/instruments/crypto_spot.json 一致。下单仍使用当次官方 instrument。"


def _decimal_text(value: str) -> str:
    if value in ("", None):
        return ""
    return format(Decimal(str(value)).normalize(), "f")


def startup_lines(prepared: dict[str, Any]) -> list[str]:
    choice: VenueChoice = prepared["choice"]
    fee = prepared["fee"]
    http_base = choice.http_base or "适配器默认官方域名"
    ws_base = choice.ws_base or "适配器默认官方域名"
    return [
        "通路测试",
        f"场所 {choice.venue} 交易对 {choice.instrument_id}",
        choice.reason,
        f"HTTP {http_base}",
        f"WebSocket {ws_base}",
        "执行客户端只有本机沙箱 PaperSandboxExecutionClient，没有交易所执行客户端。",
        "行情配置没有 API 密钥，也没有代理。",
        f"费率 {fee['fee_version']}，账户等级 {fee['account_tier']}，"
        f"挂单 {fee['maker_rate']}，吃单 {fee['taker_rate']}，检索日 {fee['retrieved_on']}。",
        "延迟 LatencyModel 100 毫秒（1.231 里还没有 StaticLatencyModel 这个类名），消息队列打开。",
        f"配置哈希 {prepared['hash']}",
        prepared["filter_note"],
        f"不平衡倍数 {prepared['imbalance']}，会话 {prepared['seconds']} 秒。",
    ]


def run_session(root: Path, run_dir: Path, *, resume: bool, seconds: int | None, imbalance: str | None) -> Path:
    prepared = prepare_run(root, run_dir, resume=resume, seconds=seconds, imbalance=imbalance)
    for line in startup_lines(prepared):
        print(line)
    node = build_node(
        run_dir=run_dir,
        paper=prepared["paper"],
        fee=prepared["fee"],
        choice=prepared["choice"],
        starting_balances=prepared["balances"],
        session_seconds=prepared["seconds"],
        imbalance=prepared["imbalance"],
    )
    try:
        print("执行客户端：" + ", ".join(type(client).__name__ for client in node.kernel.exec_engine._clients.values()))
        node.run(raise_exception=True)
        _store_instrument(node, run_dir, prepared["choice"].instrument_id)
    finally:
        if runtime.CURRENT is not None:
            runtime.CURRENT.gate.flush()
        node.dispose()
    write_outputs(run_dir, replay=True)
    return run_dir


def check_session(root: Path, run_dir: Path) -> None:
    prepared = prepare_run(root, run_dir, resume=False, seconds=0, imbalance=None)
    for line in startup_lines(prepared):
        print(line)
    node = build_node(
        run_dir=run_dir,
        paper=prepared["paper"],
        fee=prepared["fee"],
        choice=prepared["choice"],
        starting_balances=prepared["balances"],
        session_seconds=0,
        imbalance=prepared["imbalance"],
    )
    try:
        names = [type(client).__name__ for client in node.kernel.exec_engine._clients.values()]
        factories = list(node._builder._exec_factories)
        print("自检通过。")
        print("已注册执行客户端：" + ", ".join(names))
        print("已注册执行工厂：" + ", ".join(factories))
        print("没有调用 node.run()，没有发单。")
    finally:
        node.dispose()


def _store_instrument(node, run_dir: Path, instrument_id: str) -> None:
    instrument = node.cache.instrument(InstrumentId.from_str(instrument_id))
    if instrument is None:
        return
    catalog = ParquetDataCatalog(str((run_dir / "catalog").resolve()))
    catalog.write_data([instrument], skip_disjoint_check=True)


def _query(catalog: ParquetDataCatalog, data_cls: type) -> list:
    try:
        found = catalog.query(data_cls)
    except Exception:
        return []
    return list(found or [])


def load_tape(run_dir: Path) -> dict[str, Any]:
    catalog = ParquetDataCatalog(str((run_dir / "catalog").resolve()))
    gate = GapGate(path=run_dir / "gaps.json")
    if gate.path is not None and gate.path.is_file():
        saved = json.loads(gate.path.read_text(encoding="utf-8"))
        gate.gaps = [tuple(item) for item in saved.get("gaps", [])]
        gate.stale_ns = int(saved.get("stale_ns", gate.stale_ns))
    quotes = _query(catalog, QuoteTick)
    trades = _query(catalog, TradeTick)
    books = _query(catalog, OrderBookDeltas)
    instruments = _query(catalog, CurrencyPair)
    visible = [
        item
        for item in [*books, *trades, *quotes]
        if not gate.blocks(item.ts_event, item.ts_init)
    ]
    visible.sort(key=_data_sort_key)
    return {
        "quotes": quotes,
        "trades": trades,
        "books": books,
        "instruments": instruments,
        "visible": visible,
    }


def write_outputs(run_dir: Path, *, replay: bool) -> dict[str, Any]:
    snapshot = _read_json(run_dir / "config_snapshot.json")
    ledger_path = run_dir / "ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.is_file() else {"fills": []}
    tape = load_tape(run_dir)
    quotes = quote_frame(tape["quotes"])
    trades = trade_frame(tape["trades"])
    quality = data_quality(quotes, trades)
    fills = ledger.get("fills") or []
    book = build_fill_book(fills, quotes, int(snapshot["matching"]["latency_ns"]))
    rate = participation(book, trades)
    report_dir = run_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    write_fill_book(book, report_dir)
    starting = Decimal(str(snapshot["matching"]["starting_balance"]).split()[0])
    equity = equity_curve(quotes, fills, starting)
    daily = write_daily_summary(report_dir / "daily_returns.txt", equity, book, starting)
    note = write_quantstats(daily, report_dir / "quantstats.html")
    (report_dir / "quantstats_note.txt").write_text(
        (note or "已生成 quantstats.html。") + "\n",
        encoding="utf-8",
    )
    replay_value = None
    live_diff = None
    replay_error = None
    if replay and tape["instruments"]:
        try:
            instrument = _matching_instrument(tape["instruments"], snapshot)
            config = _replay_config(snapshot, run_dir)
            first = run_backtest(instrument, tape["visible"], config)
            second = run_backtest(instrument, tape["visible"], config)
            replay_value = diff_count(first, second)
            live_keys = [
                (fill["side"], fill["quantity"], fill["price"])
                for fill in fills
            ]
            live_diff = diff_count(live_keys, first)
        except Exception as exc:
            replay_error = f"{type(exc).__name__}: {exc}"
    meta = _read_json(run_dir / "run_meta.json") if (run_dir / "run_meta.json").is_file() else {"attempts": []}
    seconds = meta["attempts"][-1]["session_seconds"] if meta.get("attempts") else 0
    verdict = acceptance(
        quality=quality,
        fill_book=book,
        participation_rate=rate,
        replay_diff=replay_value,
        live_diff=live_diff,
        session_seconds=int(seconds),
    )
    header = {
        "title": "通路测试",
        "config_hash": snapshot["hash"],
        "matching": snapshot["matching"],
        "fee_source": snapshot["fee"].get("source"),
        "fee_source_note": snapshot["fee"].get("source_note"),
        "venue": snapshot["venue"],
        "nautilus": snapshot.get("nautilus"),
        "filter_note": filter_note_from_snapshot(snapshot),
        "data_quality": quality,
        "participation": None if rate is None else str(rate),
        "replay_diff": replay_value,
        "replay_error": replay_error,
        "live_vs_replay_diff": live_diff,
        "quantstats": note or "quantstats.html",
    }
    _write_json(report_dir / "header.json", header)
    _write_json(report_dir / "data_quality.json", quality)
    _write_json(report_dir / "acceptance.json", verdict)
    print(f"报告目录 {report_dir}")
    return verdict


def filter_note_from_snapshot(snapshot: dict[str, Any]) -> str:
    return snapshot.get("filter_note") or snapshot["venue"].get("reason") or ""


def _matching_instrument(instruments: list, snapshot: dict[str, Any]):
    wanted = snapshot["matching"]["instrument_id"]
    for instrument in instruments:
        if str(instrument.id) == wanted:
            return instrument
    return instruments[-1]


def _replay_config(snapshot: dict[str, Any], run_dir: Path) -> ConnectivityConfig:
    matching = snapshot["matching"]
    return ConnectivityConfig(
        instrument_id=matching["instrument_id"],
        order_quote_notional=matching["order_quote_notional"],
        max_notional=matching["max_notional_per_order"],
        imbalance_multiple=matching["imbalance_multiple"],
        protection_band=matching["protection_band"],
        session_seconds=0,
        use_exchange_depth_params=False,
        maker_fee=matching["maker_rate"],
        taker_fee=matching["taker_rate"],
        fee_version=matching["fee_version"],
        ledger_path="",
        starting_balance=matching["starting_balance"],
        latency_ns=int(matching["latency_ns"]),
    )


def replay_session(run_dir: Path) -> int:
    verdict = write_outputs(run_dir, replay=True)
    count = verdict["replay_diff_zero"]["replay_diff"]
    print(f"两次重放的价量差异笔数 {count}")
    if count is None:
        raise ConfigError("目录里没有品种，不能重放。先跑一次会话。")
    return int(count)
