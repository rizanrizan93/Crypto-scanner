from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


def decimal_required(value: object, field: str) -> Decimal:
    if value in (None, ""):
        raise ValueError(f"missing required decimal field: {field}")
    return Decimal(str(value))


def decimal_optional(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class InstrumentInfo:
    symbol: str
    status: str
    contract_type: str
    base_coin: str
    quote_coin: str
    settle_coin: str
    tick_size: Decimal
    min_order_qty: Decimal
    qty_step: Decimal
    min_notional_value: Decimal | None
    max_order_qty: Decimal | None
    max_market_order_qty: Decimal | None
    min_leverage: Decimal | None
    max_leverage: Decimal | None
    leverage_step: Decimal | None


@dataclass(frozen=True, slots=True)
class TickerSnapshot:
    symbol: str
    last_price: Decimal
    mark_price: Decimal
    index_price: Decimal
    bid_price: Decimal
    ask_price: Decimal
    bid_size: Decimal | None
    ask_size: Decimal | None
    volume_24h: Decimal | None
    turnover_24h: Decimal | None
    open_interest: Decimal | None
    open_interest_value: Decimal | None
    funding_rate: Decimal | None
    next_funding_time_ms: int | None

    @property
    def spread(self) -> Decimal:
        return self.ask_price - self.bid_price

    @property
    def mid_price(self) -> Decimal:
        return (self.ask_price + self.bid_price) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal:
        mid = self.mid_price
        if mid <= 0:
            raise ValueError("mid price must be positive")
        return self.spread / mid * Decimal("10000")


@dataclass(frozen=True, slots=True)
class Candle:
    start_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal


@dataclass(frozen=True, slots=True)
class OpenInterestPoint:
    timestamp_ms: int
    open_interest: Decimal


@dataclass(frozen=True, slots=True)
class FundingRatePoint:
    timestamp_ms: int
    funding_rate: Decimal


@dataclass(frozen=True, slots=True)
class PublicTrade:
    symbol: str
    timestamp_ms: int
    side: str
    price: Decimal
    size: Decimal
    trade_id: str

    @property
    def signed_size(self) -> Decimal:
        if self.side == "Buy":
            return self.size
        if self.side == "Sell":
            return -self.size
        raise ValueError(f"unexpected taker side: {self.side}")


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class OrderBookUpdate:
    symbol: str
    update_type: str
    timestamp_ms: int
    sequence: int | None
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
