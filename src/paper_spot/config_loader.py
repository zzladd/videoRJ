"""读取版本化配置并计算报告抬头用的哈希。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from paper_spot.constants import OFFICIAL_HOSTS


class ConfigError(RuntimeError):
    pass


def repo_root() -> Path:
    for start in (Path.cwd(), Path(__file__).resolve()):
        for parent in (start, *start.parents):
            if (parent / "config" / "paper.json").is_file():
                return parent
    raise ConfigError("找不到 config/paper.json。请在仓库根目录运行。")


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ConfigError(f"{path} 必须是 JSON 对象")
    return value


def load_paper(root: Path | None = None) -> tuple[Path, dict[str, Any]]:
    root = root or repo_root()
    path = root / "config" / "paper.json"
    return path, load_json(path)


def fee_path(root: Path, venue: str) -> Path:
    name = {
        "BINANCE": "binance_spot_regular.json",
        "BYBIT": "bybit_spot_vip0.json",
        "OKX": "okx_spot_regular.json",
    }.get(venue)
    if name is None:
        raise ConfigError(f"没有 {venue} 的现货费率文件")
    return root / "config" / "fees" / name


def load_fee(root: Path, venue: str) -> dict[str, Any]:
    document = load_json(fee_path(root, venue))
    maker = Decimal(document["maker_rate"])
    taker = Decimal(document["taker_rate"])
    if maker <= 0 or taker <= 0:
        raise ConfigError(f"{venue} 的挂单或吃单费率不是正数。不允许用 0 填缺口。")
    if document.get("venue") != venue:
        raise ConfigError(f"费率文件场所与 {venue} 不一致")
    for field in ("fee_version", "account_tier", "retrieved_on", "source"):
        if not document.get(field):
            raise ConfigError(f"费率文件缺少 {field}")
    return document


def load_instrument_master(root: Path) -> dict[str, Any]:
    return load_json(root / "config" / "instruments" / "crypto_spot.json")


def load_domestic(root: Path) -> dict[str, Any]:
    document = load_json(root / "config" / "instruments" / "domestic.json")
    if document.get("instruments"):
        raise ConfigError("第一期国内品种主数据必须为空")
    return document


def canonical_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def assert_official_url(url: str | None) -> None:
    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "wss"}:
        raise ConfigError(f"只允许官方 https/wss 接入点，收到 {parsed.scheme or '空协议'}")
    host = parsed.hostname or ""
    if host not in OFFICIAL_HOSTS:
        raise ConfigError(f"接入点 {host} 不在官方域名表内")
    if parsed.username or parsed.password:
        raise ConfigError("接入点里不能带用户名或密码")
