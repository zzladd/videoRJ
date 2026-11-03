"""只在本机打开一次运行目录。页面上的数都来自这个目录，不拉外部行情。"""

from __future__ import annotations

import html
import json
from decimal import Decimal
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

from paper_spot.config_loader import ConfigError
from paper_spot.config_loader import repo_root
from paper_spot.reports import build_fill_book
from paper_spot.reports import equity_curve
from paper_spot.reports import participation
from paper_spot.reports import quote_frame
from paper_spot.reports import trade_frame

_HOST = "127.0.0.1"


def bind_server(run_dir: Path, port: int = 8765) -> ThreadingHTTPServer:
    """只监听 127.0.0.1。调用方负责 serve_forever 和关闭。"""
    if port < 0 or port > 65535:
        raise ConfigError("端口要在 0 到 65535 之间")
    page = render_page(run_dir)
    try:
        server = ThreadingHTTPServer((_HOST, port), _handler(page))
    except OSError as exc:
        raise ConfigError(f"127.0.0.1:{port} 无法监听：{exc}") from exc
    bound_host = server.server_address[0]
    if bound_host not in {_HOST, "localhost"}:
        server.server_close()
        raise ConfigError(f"查看页拒绝绑定 {bound_host}")
    return server


def serve_run(run_dir: Path, port: int = 8765) -> None:
    """打开本机站点并落到这一次运行。仍只绑定 127.0.0.1。"""
    from paper_spot.site import serve_site

    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        raise ConfigError(f"没有这个运行目录：{run_dir}")
    if not (run_dir / "ledger.json").is_file():
        raise ConfigError(f"{run_dir / 'ledger.json'} 不存在。先跑一次 paper_spot run。")
    serve_site(port=port, root=repo_root(), runs_dir=run_dir.parent, focus_run=run_dir.name)


def render_page(run_dir: Path) -> str:
    model = build_model(run_dir)
    return _standalone_html(model)


def render_run_body(model: dict[str, Any]) -> str:
    """运行详情的图和表，给本机站点套壳用。"""
    empty = model["empty"]
    banner = ""
    if empty:
        banner = "<p class='empty' id='empty-state'>这次运行没有成交。下面的图没有成交样本。</p>"
    fee_bps = _bps(model["fee_total"], model["notional_total"])
    notes = _notes(model, fee_bps)
    return f"""
<p class="meta">目录 {html.escape(model["run_name"])}　交易对 {html.escape(str(model["instrument"] or "未知"))}　策略 {html.escape(str(model["strategy"]))}。只展示这次运行已经写下的数，没有外部行情，也不是买卖建议。</p>
{banner}
<p>起始现金 {_num(model["starting"])} USDT。结束现金 {_num(model["ending_cash"])} USDT。结束时基础币 {_num(model["base_left"])}。</p>
<section id="equity">
<h2>权益曲线</h2>
<p>每笔成交之后，用当时的中间价给剩余基础币计价。没有行情中间价时，用成交价自己。</p>
{_line_svg(model["equity"], "权益 USDT")}
</section>
<section id="realized">
<h2>已实现收益</h2>
<p>买入只累积成本（含买入费用）。卖出时结算这一段。持仓还在时，曲线不把浮盈算成已实现。合计 {_num(model["realized_total"])} USDT。</p>
{_line_svg(model["realized"], "已实现 USDT")}
</section>
<section id="fills">
<h2>每笔成交</h2>
{_fill_table(model["fills"])}
<h3>价格</h3>
{_dot_svg(model["fills"])}
<h3>费用</h3>
{_bar_svg([(row["time"], row["fee"]) for row in model["fills"]], "费用 USDT")}
</section>
<section id="arrival">
<h2>到达成本</h2>
<p>买入是 (成交价 - 决策中间价) / 中间价，卖出反过来，单位是基点。正数表示这次成交比决策中间价更差。</p>
{_bar_svg([(row["time"], row["arrival"]) for row in model["fills"] if row["arrival"] is not None], "到达成本 bps", zero=True)}
</section>
<section id="participation">
<h2>参与率</h2>
{_participation_svg(model)}
</section>
<section id="improve">
<h2>可改进方向</h2>
<p>下面每条都只对照这次目录里的数。条形是为了看见相对长短，不是外部评级。</p>
{notes}
</section>
"""


def build_model(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        raise ConfigError(f"没有这个运行目录：{run_dir}")
    ledger_path = run_dir / "ledger.json"
    if not ledger_path.is_file():
        raise ConfigError(f"{ledger_path} 不存在。先跑一次 paper_spot run。")
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    fills = list(ledger.get("fills") or [])
    orders = list(ledger.get("orders") or [])
    snapshot = _read_json(run_dir / "config_snapshot.json")
    acceptance = _read_json(run_dir / "report" / "acceptance.json")
    header = _read_json(run_dir / "report" / "header.json")
    starting = _starting_usdt(snapshot, ledger)
    quotes = _load_quotes(run_dir)
    quote_rows = quote_frame(quotes)
    book = _fill_book(run_dir, fills, quote_rows, snapshot)
    rate = _participation(acceptance, book, run_dir)
    warn, warn_source = _participation_warn(snapshot)
    equity = equity_curve(quote_rows, fills, starting)
    realized = realized_curve(fills)
    ending_cash = _ending_cash(ledger, starting)
    base_left = Decimal(str((ledger.get("balances") or {}).get("BTC", "0")))
    return {
        "run_name": run_dir.name,
        "instrument": (snapshot.get("matching") or {}).get("instrument_id") or "",
        "strategy": snapshot.get("strategy") or "connectivity",
        "starting": starting,
        "ending_cash": ending_cash,
        "base_left": base_left,
        "fills": _fill_rows(fills, book),
        "equity": [(item.isoformat(), float(value)) for item, value in equity.items()],
        "realized": [( _fmt_ts(item[0]), float(item[1])) for item in realized],
        "realized_total": realized[-1][1] if realized else Decimal(0),
        "participation": rate,
        "warn": warn,
        "warn_source": warn_source,
        "replay": _replay(acceptance),
        "fill_rate": _fill_rate(orders, fills),
        "fee_total": _fee_total(book),
        "notional_total": _notional_total(book),
        "arrival_mean": _mean_decimal(book, "arrival_cost_bps"),
        "arrival_max": _max_abs(book, "arrival_cost_bps"),
        "header_title": header.get("title") or "通路测试",
        "empty": len(fills) == 0,
    }


def realized_curve(fills: list[dict[str, Any]]) -> list[tuple[int, Decimal]]:
    """平仓时结算。持仓成本含买入费用。结束时若已平仓，合计等于现金变化。"""
    base = Decimal(0)
    cost = Decimal(0)
    realized = Decimal(0)
    points: list[tuple[int, Decimal]] = []
    for fill in sorted(fills, key=lambda item: int(item["ts_init"])):
        quantity = Decimal(str(fill["quantity"]))
        price = Decimal(str(fill["price"]))
        fee = Decimal(str(fill["commission"]).split()[0])
        if fill["side"] == "BUY":
            cost += quantity * price + fee
            base += quantity
        else:
            sold = min(quantity, base) if base > 0 else Decimal(0)
            if sold > 0:
                average = cost / base
                realized += sold * price - fee - average * sold
                cost -= average * sold
                base -= sold
                if base == 0:
                    cost = Decimal(0)
            else:
                realized -= fee
        points.append((int(fill["ts_init"]), realized))
    return points


def _handler(page: str):
    body = page.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path not in {"/", "/index.html"}:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def _standalone_html(model: dict[str, Any]) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>通路测试 {html.escape(model["run_name"])}</title>
<style>
{_PAGE_CSS}
</style>
</head>
<body>
<main>
<h1>{html.escape(str(model["header_title"]))} 查看</h1>
{render_run_body(model)}
</main>
</body>
</html>
"""


_PAGE_CSS = """
body { margin: 0; font-family: "IBM Plex Sans", "Noto Sans SC", sans-serif; background: linear-gradient(180deg, #d9e4ef 0%, #eef2f6 220px, #eef2f6 100%); color: #132033; }
main { max-width: 980px; margin: 0 auto; padding: 28px 20px 64px; }
h1 { font-size: 28px; margin: 0 0 8px; letter-spacing: -0.02em; }
h2 { font-size: 18px; margin: 0 0 8px; }
h3 { font-size: 15px; margin: 18px 0 8px; color: #3b4a5c; }
p { line-height: 1.55; }
.meta { color: #5a6a7c; }
.empty { background: #fff4d6; border: 1px solid #e0c36a; padding: 12px 14px; border-radius: 8px; }
section { background: rgba(255,255,255,.92); border: 1px solid #d7e0ea; border-radius: 12px; padding: 16px 18px 20px; margin-top: 14px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #e6edf4; font-size: 14px; }
.note { display: grid; grid-template-columns: 140px 1fr; gap: 8px 16px; padding: 10px 0; border-top: 1px solid #e6edf4; }
.note b { font-size: 14px; }
.bar { height: 10px; background: #d9e3ee; border-radius: 99px; margin-top: 6px; }
.bar span { display: block; height: 10px; border-radius: 99px; background: #0f766e; }
.watch span { background: #b45309; }
svg { width: 100%; height: auto; }
.axis { fill: #5a6a7c; font-size: 12px; }
"""


def _notes(model: dict[str, Any], fee_bps: Decimal | None) -> str:
    blocks = []
    if model["empty"]:
        blocks.append(_note("费用拖累", "没有样本", "没有成交，就没有费用。", None, "empty"))
        blocks.append(_note("到达成本", "没有样本", "没有成交，就没有到达成本。", None, "empty"))
        blocks.append(_note("成交率", "没有样本", "账本里没有成交。", None, "empty"))
    else:
        if fee_bps is None:
            blocks.append(_note("费用拖累", "没有数", "成交名义金额是 0，费用基点算不出来。", None, "empty"))
        else:
            blocks.append(
                _note(
                    "费用拖累",
                    f"{_num(fee_bps)} bps",
                    f"费用合计 {_num(model['fee_total'])} USDT，成交名义金额 {_num(model['notional_total'])} USDT。条形在 20 个基点时画满，只用来和到达成本比长短。",
                    _ratio(fee_bps, Decimal(20)),
                    "ok",
                )
            )
        arrival = model["arrival_mean"]
        if arrival is None:
            blocks.append(_note("到达成本", "没有数", "成交簿里没有到达成本。", None, "empty"))
        else:
            direction = "比决策中间价更差" if arrival > 0 else "没有比决策中间价更差"
            if arrival == 0:
                direction = "和决策中间价相同"
            blocks.append(
                _note(
                    "到达成本",
                    f"均值 {_num(arrival)} bps",
                    f"这次均值{direction}。绝对值最大的一笔是 {_num(model['arrival_max'])} bps。",
                    _ratio(abs(arrival), Decimal(20)),
                    "watch" if arrival > 0 else "ok",
                )
            )
        rate = model["fill_rate"]
        if rate is None:
            blocks.append(_note("成交率", "没有订单列表", "账本没有 orders，成交率留空。", None, "empty"))
        else:
            blocks.append(
                _note(
                    "成交率",
                    f"{_num(rate * 100)}%",
                    "成交的订单数除以提交的订单数。低于 100% 表示有订单被取消或没有成交。",
                    rate,
                    "watch" if rate < 1 else "ok",
                )
            )
    blocks.append(_participation_note(model))
    blocks.append(_replay_note(model["replay"]))
    return "".join(blocks)


def _participation_note(model: dict[str, Any]) -> str:
    rate = model["participation"]
    warn = model["warn"]
    if rate is None:
        return _note("参与率", "没有数", "这次目录没有参与率。没有成交，或没有市场成交量。", None, "empty")
    source = model["warn_source"] or "没有警戒线"
    if warn is None or warn <= 0:
        return _note("参与率", _num(rate), f"影子成交量 / 市场成交量。{source}，所以不标接近警戒。", None, "ok")
    if rate >= warn:
        relation = "高于"
        tone = "watch"
    elif rate >= warn / 2:
        relation = "接近"
        tone = "watch"
    else:
        relation = "低于"
        tone = "ok"
    return _note(
        "参与率",
        _num(rate),
        f"{relation}警戒线 {_num(warn)}。警戒线来自{source}。这只说明这次成交量相对市场成交量的大小。",
        _ratio(rate, warn),
        tone,
    )


def _replay_note(replay: dict[str, Any]) -> str:
    if not replay.get("present"):
        return _note("回放是否一致", "还没有回放", "这个目录里没有回放结果。先跑 paper_spot replay。", None, "empty")
    live = replay.get("live_diff")
    again = replay.get("replay_diff")
    if again == 0 and live in (0, None) and replay.get("met"):
        detail = "两次重放的价量差异是 0"
        if live == 0:
            detail += "，和这次影子账本的价量差异也是 0。"
        else:
            detail += "。这次没有单独记下和账本的差异。"
        return _note("回放是否一致", "一致", detail, Decimal(1), "ok")
    return _note(
        "回放是否一致",
        "不一致",
        f"两次重放差异 {again}，和账本差异 {live}。先不要把这组价量当成已经对上。",
        Decimal(0),
        "watch",
    )


def _note(title: str, value: str, detail: str, ratio: Decimal | None, tone: str) -> str:
    bar = ""
    if ratio is not None:
        width = max(0, min(100, float(ratio) * 100))
        bar = f"<div class='bar {tone}'><span style='width:{width:.2f}%'></span></div>"
    return (
        f"<div class='note'><div><b>{html.escape(title)}</b><div>{html.escape(value)}</div></div>"
        f"<div>{html.escape(detail)}{bar}</div></div>"
    )


def _participation_svg(model: dict[str, Any]) -> str:
    rate = model["participation"]
    warn = model["warn"]
    if rate is None:
        return "<p>这次没有参与率。</p>"
    cap = warn if warn and warn > 0 else rate if rate > 0 else Decimal(1)
    width = 0 if cap <= 0 else float(rate / cap) * 100
    width = max(0, min(100, width))
    label = f"参与率 {_num(rate)}"
    if warn and warn > 0:
        label += f"　警戒 {_num(warn)} 在条的右端"
    return (
        f"<p>{html.escape(label)}。来源：{html.escape(model['warn_source'] or '这次报告')}。</p>"
        f"<div class='bar'><span style='width:{width:.2f}%'></span></div>"
    )


def _line_svg(points: list[tuple[str, float]], y_label: str) -> str:
    if not points:
        return f"<p>没有点。{html.escape(y_label)}画不出来。</p>"
    values = [item[1] for item in points]
    return _plot(values, y_label, kind="line")


def _dot_svg(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>没有成交价格。</p>"
    values = [row["price"] for row in rows]
    colors = ["#2f6f4e" if row["side"] == "BUY" else "#8c3b2a" for row in rows]
    return _plot(values, "价格", kind="dots", colors=colors)


def _bar_svg(pairs: list[tuple[str, float | None]], y_label: str, zero: bool = False) -> str:
    values = [item[1] for item in pairs if item[1] is not None]
    if not values:
        return f"<p>没有{html.escape(y_label)}。</p>"
    return _plot([float(item) for item in values], y_label, kind="bars", zero=zero)


def _plot(values: list[float], y_label: str, kind: str, colors: list[str] | None = None, zero: bool = False) -> str:
    width, height, left, right, top, bottom = 760, 236, 112, 16, 28, 32
    low = min(values)
    high = max(values)
    if zero:
        low = min(low, 0.0)
        high = max(high, 0.0)
    if high == low:
        pad = 1 if high == 0 else abs(high) * 0.05
        low -= pad
        high += pad
    span = high - low
    inner_w = width - left - right
    inner_h = height - top - bottom

    def x_at(index: int) -> float:
        if len(values) == 1:
            return left + inner_w / 2
        return left + inner_w * index / (len(values) - 1)

    def y_at(value: float) -> float:
        return top + (high - value) / span * inner_h

    parts = [
        f"<svg viewBox='0 0 {width} {height}' role='img'>",
        f"<text class='axis' x='{left}' y='16'>{html.escape(y_label)}</text>",
        f"<text class='axis' text-anchor='end' x='{left - 8}' y='{top + 11}'>{html.escape(_num(Decimal(str(high))))}</text>",
        f"<text class='axis' text-anchor='end' x='{left - 8}' y='{top + inner_h}'>{html.escape(_num(Decimal(str(low))))}</text>",
    ]
    if zero and low < 0 < high:
        y0 = y_at(0)
        parts.append(f"<line x1='{left}' y1='{y0:.2f}' x2='{width - right}' y2='{y0:.2f}' stroke='#d9d3c7'/>")
    if kind == "line":
        coords = " ".join(f"{x_at(i):.2f},{y_at(value):.2f}" for i, value in enumerate(values))
        parts.append(f"<polyline fill='none' stroke='#245c45' stroke-width='2' points='{coords}'/>")
    elif kind == "dots":
        for index, value in enumerate(values):
            color = colors[index] if colors else "#245c45"
            parts.append(f"<circle cx='{x_at(index):.2f}' cy='{y_at(value):.2f}' r='5' fill='{color}'/>")
    else:
        bar_w = max(4, inner_w / max(len(values), 1) * 0.6)
        for index, value in enumerate(values):
            y = y_at(value)
            base = y_at(0 if zero else low)
            top_y = min(y, base)
            parts.append(
                f"<rect x='{x_at(index) - bar_w / 2:.2f}' y='{top_y:.2f}' width='{bar_w:.2f}' "
                f"height='{abs(base - y):.2f}' fill='#3d6f8c'/>"
            )
    parts.append("</svg>")
    return "".join(parts)


def _fill_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>没有成交。</p>"
    body = []
    for row in rows:
        arrival = "—" if row["arrival"] is None else _num(Decimal(str(row["arrival"])))
        body.append(
            "<tr>"
            f"<td>{html.escape(row['time'])}</td>"
            f"<td>{html.escape(row['side'])}</td>"
            f"<td>{_num(Decimal(str(row['price'])))}</td>"
            f"<td>{_num(Decimal(str(row['fee'])))}</td>"
            f"<td>{arrival}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>时间 (UTC)</th><th>方向</th><th>价格</th><th>费用</th><th>到达成本 bps</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _fill_rows(fills: list[dict[str, Any]], book) -> list[dict[str, Any]]:
    arrivals = {}
    if book is not None and not book.empty and "arrival_cost_bps" in book.columns:
        for _, row in book.iterrows():
            value = row["arrival_cost_bps"]
            arrivals[str(row["trade_id"])] = None if _missing(value) else float(Decimal(str(value)))
    rows = []
    for fill in sorted(fills, key=lambda item: int(item["ts_init"])):
        rows.append(
            {
                "time": _fmt_ts(int(fill["ts_event"])),
                "side": str(fill["side"]),
                "price": float(Decimal(str(fill["price"]))),
                "fee": float(Decimal(str(fill["commission"]).split()[0])),
                "arrival": arrivals.get(str(fill["trade_id"])),
            }
        )
    return rows


def _fill_book(run_dir: Path, fills: list[dict[str, Any]], quotes, snapshot: dict[str, Any]):
    path = run_dir / "report" / "fill_book.csv"
    if path.is_file():
        import pandas as pd

        frame = pd.read_csv(path)
        if not frame.empty or not fills:
            return frame
    latency = int((snapshot.get("matching") or {}).get("latency_ns") or 100_000_000)
    return build_fill_book(fills, quotes, latency)


def _participation(acceptance: dict[str, Any], book, run_dir: Path) -> Decimal | None:
    block = (acceptance.get("participation_fee_arrival") or {}) if acceptance else {}
    raw = block.get("participation")
    if raw not in (None, "", "None"):
        return Decimal(str(raw))
    trades = _load_trades(run_dir)
    if not trades:
        return None
    return participation(book, trade_frame(trades))


def _participation_warn(snapshot: dict[str, Any]) -> tuple[Decimal | None, str]:
    raw = snapshot.get("participation_warn")
    if raw not in (None, ""):
        return Decimal(str(raw)), "这次运行快照里的 participation_warn"
    paper_path = repo_root() / "config" / "paper.json"
    if paper_path.is_file():
        paper = json.loads(paper_path.read_text(encoding="utf-8"))
        if paper.get("participation_warn") not in (None, ""):
            return Decimal(str(paper["participation_warn"])), "当前 config/paper.json 的 participation_warn（这次快照没写这项）"
    return None, ""


def _replay(acceptance: dict[str, Any]) -> dict[str, Any]:
    block = acceptance.get("replay_diff_zero") if acceptance else None
    if not block or block.get("replay_diff") is None and block.get("live_vs_replay_diff") is None:
        return {"present": False}
    return {
        "present": True,
        "met": bool(block.get("met")),
        "replay_diff": block.get("replay_diff"),
        "live_diff": block.get("live_vs_replay_diff"),
    }


def _fill_rate(orders: list[dict[str, Any]], fills: list[dict[str, Any]]) -> Decimal | None:
    if not orders:
        return None
    filled = {str(item.get("client_order_id")) for item in fills}
    submitted = [str(item.get("client_order_id")) for item in orders]
    hit = sum(1 for item in submitted if item in filled)
    return Decimal(hit) / Decimal(len(submitted))


def _fee_total(book) -> Decimal:
    if book is None or book.empty or "commission" not in book.columns:
        return Decimal(0)
    return sum((Decimal(str(value).split()[0]) for value in book["commission"]), Decimal(0))


def _notional_total(book) -> Decimal:
    if book is None or book.empty or "notional_quote" not in book.columns:
        return Decimal(0)
    return sum((Decimal(str(value)) for value in book["notional_quote"]), Decimal(0))


def _mean_decimal(book, column: str) -> Decimal | None:
    if book is None or book.empty or column not in book.columns:
        return None
    values = [Decimal(str(value)) for value in book[column] if not _missing(value)]
    if not values:
        return None
    return sum(values, Decimal(0)) / Decimal(len(values))


def _max_abs(book, column: str) -> Decimal | None:
    if book is None or book.empty or column not in book.columns:
        return None
    values = [abs(Decimal(str(value))) for value in book[column] if not _missing(value)]
    if not values:
        return None
    return max(values)


def _load_quotes(run_dir: Path) -> list:
    return _query(run_dir, QuoteTick)


def _load_trades(run_dir: Path) -> list:
    from nautilus_trader.model.data import TradeTick

    return _query(run_dir, TradeTick)


def _query(run_dir: Path, data_cls: type) -> list:
    catalog_dir = run_dir / "catalog"
    if not catalog_dir.is_dir():
        return []
    try:
        catalog = ParquetDataCatalog(str(catalog_dir.resolve()))
        found = catalog.query(data_cls)
    except Exception:
        return []
    return list(found or [])


def _starting_usdt(snapshot: dict[str, Any], ledger: dict[str, Any]) -> Decimal:
    text = (snapshot.get("matching") or {}).get("starting_balance")
    if not text:
        items = ledger.get("starting_balances") or ["10000 USDT"]
        text = items[0]
    return Decimal(str(text).split()[0])


def _ending_cash(ledger: dict[str, Any], starting: Decimal) -> Decimal:
    balances = ledger.get("balances") or {}
    if "USDT" in balances:
        return Decimal(str(balances["USDT"]))
    return starting


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _fmt_ts(ns: int) -> str:
    stamp = datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def _num(value: Decimal | None) -> str:
    if value is None:
        return "—"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _bps(fee: Decimal, notional: Decimal) -> Decimal | None:
    if notional <= 0:
        return None
    return fee / notional * Decimal(10000)


def _ratio(value: Decimal, cap: Decimal) -> Decimal:
    if cap <= 0:
        return Decimal(0)
    return value / cap


def _missing(value: Any) -> bool:
    if value is None:
        return True
    text = str(value)
    return text in {"", "nan", "None", "NaN"}
