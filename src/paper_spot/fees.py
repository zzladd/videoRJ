"""把版本化费率盖到现货品种上。MakerTakerFeeModel 读取的是品种上的费率。"""

from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.instruments import CurrencyPair


def stamp_currency_pair(
    instrument: CurrencyPair,
    maker: Decimal,
    taker: Decimal,
) -> CurrencyPair:
    if maker <= 0 or taker <= 0:
        raise ValueError("挂单费率和吃单费率都必须是正数")
    return CurrencyPair(
        instrument_id=instrument.id,
        raw_symbol=instrument.raw_symbol,
        base_currency=instrument.base_currency,
        quote_currency=instrument.quote_currency,
        price_precision=instrument.price_precision,
        size_precision=instrument.size_precision,
        price_increment=instrument.price_increment,
        size_increment=instrument.size_increment,
        multiplier=instrument.multiplier,
        lot_size=instrument.lot_size,
        max_quantity=instrument.max_quantity,
        min_quantity=instrument.min_quantity,
        max_notional=instrument.max_notional,
        min_notional=instrument.min_notional,
        max_price=instrument.max_price,
        min_price=instrument.min_price,
        margin_init=instrument.margin_init,
        margin_maint=instrument.margin_maint,
        maker_fee=maker,
        taker_fee=taker,
        ts_event=instrument.ts_event,
        ts_init=instrument.ts_init,
        info=instrument.info,
    )
