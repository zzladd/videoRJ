"""影子账本。恢复时用这里的余额，不把同一笔成交再记一次。"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any


class LedgerError(RuntimeError):
    pass


class ShadowLedger:
    def __init__(self, path: Path, starting_balances: list[str]) -> None:
        self.path = path
        if path.is_file():
            self.state = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.state = {
                "version": 1,
                "starting_balances": starting_balances,
                "balances": {},
                "fills": [],
                "orders": [],
                "trade_ids": [],
            }
            self.flush()

    @property
    def trade_ids(self) -> set[str]:
        return set(self.state["trade_ids"])

    def balances_for_start(self, fallback: list[str]) -> list[str]:
        balances: dict[str, str] = self.state.get("balances") or {}
        if not balances:
            return list(fallback)
        lines = [f"{amount} {currency}" for currency, amount in balances.items() if Decimal(amount) > 0]
        return lines or list(fallback)

    def remember_order(self, order: dict[str, Any]) -> None:
        self.state["orders"].append(order)
        self.flush()

    def apply_fill(self, fill: dict[str, Any], balances: dict[str, str]) -> None:
        trade_id = str(fill["trade_id"])
        if trade_id in self.trade_ids:
            raise LedgerError(f"成交 {trade_id} 已经入账，拒绝重复记账")
        self.state["fills"].append(fill)
        self.state["trade_ids"].append(trade_id)
        self.state["balances"] = balances
        self.flush()

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
