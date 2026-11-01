"""按名字载入 decide。不注册执行客户端。"""

from __future__ import annotations

import inspect
from decimal import Decimal
from pathlib import Path
from types import ModuleType

from paper_spot.config_loader import ConfigError
from paper_spot.strategies.api import MarketView
from paper_spot.strategies.api import OrderIntent
from paper_spot.strategies.connectivity import connectivity_decide


def normalize_strategy(name: str | None) -> str:
    text = (name or "connectivity").strip()
    if text in {"connectivity", "user"}:
        return text
    path = Path(text).expanduser()
    if path.suffix != ".py" or not path.is_file():
        raise ConfigError(
            f"找不到策略 {text}。可用 connectivity、user，或一个含 decide(market) 的 .py 文件。"
        )
    return str(path.resolve())


def load_decide(name: str | None, imbalance_multiple: Decimal):
    """返回 decide(market) -> OrderIntent | None。连通性策略使用配置里的不平衡倍数。"""
    key = normalize_strategy(name)
    if key == "connectivity":
        multiple = imbalance_multiple

        def decide(market: MarketView) -> OrderIntent | None:
            return connectivity_decide(market, multiple)

        return decide
    if key == "user":
        from paper_spot.strategies.user_strategy import decide as user_decide

        _require_decide(user_decide)
        return user_decide
    module = _load_module(Path(key))
    decide = getattr(module, "decide", None)
    _require_decide(decide)
    return decide


def _require_decide(decide) -> None:
    if not callable(decide):
        raise ConfigError("策略文件要提供函数 decide(market)")
    positional = [
        item
        for item in inspect.signature(decide).parameters.values()
        if item.kind in (item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) < 1:
        raise ConfigError("decide 至少要接收一个 market 参数")


def _load_module(path: Path) -> ModuleType:
    # 直接编译文件内容。importlib 按路径缓存源码，同一路径改完后仍会执行旧函数。
    _load_module.counter += 1
    module_name = f"paper_spot_user_strategy_{_load_module.counter}"
    module = ModuleType(module_name)
    module.__file__ = str(path)
    source = path.read_text(encoding="utf-8")
    code = compile(source, str(path), "exec")
    exec(code, module.__dict__)
    return module


_load_module.counter = 0
