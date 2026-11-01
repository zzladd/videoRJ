"""把逐笔、报价和有限深度写进 Parquet 目录。"""

from __future__ import annotations

from pathlib import Path

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog


class TapeRecorderConfig(ActorConfig, frozen=True):
    instrument_id: str
    catalog_path: str
    book_depth: int = 20
    book_update_speed_ms: int = 100
    use_exchange_depth_params: bool = True


class TapeRecorder(Actor):
    def __init__(self, config: TapeRecorderConfig) -> None:
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self._buffer: list = []
        self.catalog: ParquetDataCatalog | None = None

    def on_start(self) -> None:
        path = Path(self.config.catalog_path)
        path.mkdir(parents=True, exist_ok=True)
        self.catalog = ParquetDataCatalog(str(path.resolve()))
        params = None
        if self.config.use_exchange_depth_params:
            params = {"update_speed": self.config.book_update_speed_ms}
        self.subscribe_quote_ticks(self.instrument_id)
        self.subscribe_trade_ticks(self.instrument_id)
        self.subscribe_order_book_deltas(
            self.instrument_id,
            book_type=BookType.L2_MBP,
            depth=self.config.book_depth,
            params=params,
        )

    def on_quote_tick(self, tick) -> None:
        self._keep(tick)

    def on_trade_tick(self, tick) -> None:
        self._keep(tick)

    def on_order_book_deltas(self, deltas) -> None:
        self._keep(deltas)

    def on_stop(self) -> None:
        self.flush()

    def _keep(self, data) -> None:
        self._buffer.append(data)
        if len(self._buffer) >= 400:
            self.flush()

    def flush(self) -> None:
        if not self._buffer or self.catalog is None:
            return
        batch = self._buffer
        self._buffer = []
        batch.sort(key=lambda item: item.ts_init)
        try:
            self.catalog.write_data(batch, skip_disjoint_check=True)
        except Exception as exc:
            self.log.error(f"写入 Parquet 失败：{exc}")
            self._buffer = batch + self._buffer
