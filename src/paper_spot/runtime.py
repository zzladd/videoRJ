"""单进程会话。Nautilus 的执行客户端工厂没有额外参数，费率从这里读。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from paper_spot.gap import GapGate


@dataclass
class Session:
    fee_maker: Decimal
    fee_taker: Decimal
    fee_version: str
    gate: GapGate
    instrument_id: str


CURRENT: Session | None = None
