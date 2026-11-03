"""研究页和 /api/research 的读写。页面文件在 web/ 目录。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from paper_spot.research.intervals import INTERVAL_LABELS
from paper_spot.research.service import chart_payload
from paper_spot.research.service import coverage_report
from paper_spot.research.service import end_of_utc_date
from paper_spot.research.service import list_experiments
from paper_spot.research.service import load_experiment
from paper_spot.research.service import parse_utc_date
from paper_spot.research.service import scan_dual_ma
from paper_spot.research.service import sync_range
from paper_spot.research.service import validate_dual_ma

_WEB = Path(__file__).resolve().parents[1] / "web"
_FILES = {
    "/research/app.js": ("research.js", "text/javascript; charset=utf-8"),
    "/research/lightweight-charts.standalone.production.js": (
        "lightweight-charts.standalone.production.js",
        "text/javascript; charset=utf-8",
    ),
}


def serve_get(root: Path, path: str, query: dict[str, list[str]]) -> tuple[int, str, bytes] | None:
    if path == "/research":
        return _file("research.html", "text/html; charset=utf-8")
    if path in _FILES:
        name, content_type = _FILES[path]
        return _file(name, content_type)
    if path == "/api/research/coverage":
        return _json(coverage_report(root, _name(_one(query, "venue", "BINANCE"), "场所"), _name(_one(query, "symbol", "BTCUSDT"), "标的")))
    if path == "/api/research/experiments":
        return _json({"experiments": list_experiments(root)})
    if path == "/api/research/experiment":
        return _json(load_experiment(root, _one(query, "id", "")))
    if path == "/api/research/chart":
        start = _optional(query, "start")
        end = _optional(query, "end")
        return _json(
            chart_payload(
                root,
                _name(_one(query, "venue", "BINANCE"), "场所"),
                _name(_one(query, "symbol", "BTCUSDT"), "标的"),
                _interval(_one(query, "interval", "1d")),
                _integer(_one(query, "fast", "10"), "快线"),
                _integer(_one(query, "slow", "30"), "慢线"),
                start,
                end,
            )
        )
    if path.startswith("/api/research") or path.startswith("/research/"):
        return None
    return None


def serve_post(root: Path, path: str, body: bytes) -> tuple[int, str, bytes] | None:
    if path not in {"/api/research/sync", "/api/research/scan", "/api/research/validate"}:
        return None
    document = _read_json(body)
    venue = _name(str(document.get("venue") or "BINANCE"), "场所")
    symbol = _name(str(document.get("symbol") or "BTCUSDT"), "标的")
    interval = _interval(str(document.get("interval") or "1d"))
    if path == "/api/research/sync":
        start_ms = parse_utc_date(str(document.get("start") or ""))
        end_text = str(document.get("end") or "").strip()
        end_ms = end_of_utc_date(end_text) if end_text else _today_end()
        return _json(sync_range(root, venue, symbol, interval, start_ms, end_ms))
    if path == "/api/research/scan":
        return _json(
            scan_dual_ma(
                root,
                venue,
                symbol,
                interval,
                str(document.get("start") or ""),
                str(document.get("end") or ""),
            )
        )
    fast = _integer(document.get("fast"), "快线")
    slow = _integer(document.get("slow"), "慢线")
    return _json(
        validate_dual_ma(
            root,
            venue,
            symbol,
            interval,
            fast,
            slow,
            str(document.get("search_start") or ""),
            str(document.get("search_end") or ""),
            str(document.get("valid_start") or ""),
            str(document.get("valid_end") or ""),
        )
    )


def _file(name: str, content_type: str) -> tuple[int, str, bytes]:
    path = _WEB / name
    if not path.is_file():
        raise ValueError(f"缺少页面文件 {name}")
    return 200, content_type, path.read_bytes()


def _json(payload: dict[str, Any]) -> tuple[int, str, bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return 200, "application/json; charset=utf-8", body


def _read_json(body: bytes) -> dict[str, Any]:
    if len(body) > 65_536:
        raise ValueError("请求体过大")
    if not body:
        raise ValueError("请求体是空的")
    try:
        document = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("请求体不是 JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("请求体要是 JSON 对象")
    return document


def _one(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key)
    if not values or values[0] == "":
        return default
    return values[0]


def _optional(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values or not values[0].strip():
        return None
    return values[0].strip()


def _name(value: str, label: str) -> str:
    text = value.strip().upper()
    if not text or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for char in text):
        raise ValueError(f"{label}只能是大写字母和数字")
    return text


def _interval(value: str) -> str:
    text = value.strip()
    if text not in INTERVAL_LABELS:
        raise ValueError(f"不支持的周期：{text}")
    return text


def _integer(value: object, label: str) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}要是整数") from exc
    if number < 1 or number > 600:
        raise ValueError(f"{label}要在 1 到 600 之间")
    return number


def _today_end() -> int:
    from datetime import datetime, timezone

    moment = datetime.now(timezone.utc)
    start = datetime(moment.year, moment.month, moment.day, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000) + 86_400_000 - 1
