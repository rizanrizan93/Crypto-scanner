from __future__ import annotations

from decimal import Decimal

from crypto_scanner.bybit.models import OrderBookLevel, OrderBookUpdate


class OrderBookStateError(RuntimeError):
    """Raised when a local orderbook cannot be trusted."""


class LocalOrderBook:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self.last_update_id: int | None = None
        self.last_sequence: int | None = None
        self.last_timestamp_ms: int | None = None
        self.last_engine_timestamp_ms: int | None = None
        self.initialized = False

    def apply(self, update: OrderBookUpdate) -> None:
        if update.symbol != self.symbol:
            raise OrderBookStateError(
                f"symbol mismatch: expected {self.symbol}, received {update.symbol}"
            )

        reset = update.update_type == "snapshot" or update.update_id == 1
        if reset:
            self._bids.clear()
            self._asks.clear()
            self._apply_levels(self._bids, update.bids)
            self._apply_levels(self._asks, update.asks)
            self.initialized = True
        else:
            if not self.initialized:
                raise OrderBookStateError("delta received before initial snapshot")
            if self.last_update_id is not None and update.update_id <= self.last_update_id:
                raise OrderBookStateError("stale or duplicate orderbook update id")
            if (
                self.last_sequence is not None
                and update.sequence is not None
                and update.sequence <= self.last_sequence
            ):
                raise OrderBookStateError("stale or duplicate orderbook cross sequence")
            self._apply_levels(self._bids, update.bids)
            self._apply_levels(self._asks, update.asks)

        self.last_update_id = update.update_id
        self.last_sequence = update.sequence
        self.last_timestamp_ms = update.timestamp_ms
        self.last_engine_timestamp_ms = update.engine_timestamp_ms
        self._validate_book()

    @staticmethod
    def _apply_levels(book: dict[Decimal, Decimal], levels: tuple[OrderBookLevel, ...]) -> None:
        for level in levels:
            if level.price <= 0:
                raise OrderBookStateError("orderbook price must be positive")
            if level.size < 0:
                raise OrderBookStateError("orderbook size cannot be negative")
            if level.size == 0:
                book.pop(level.price, None)
            else:
                book[level.price] = level.size

    def _validate_book(self) -> None:
        if not self._bids or not self._asks:
            raise OrderBookStateError("orderbook must contain both bid and ask liquidity")
        if self.best_bid >= self.best_ask:
            raise OrderBookStateError("crossed or locked orderbook detected")

    @property
    def best_bid(self) -> Decimal:
        if not self._bids:
            raise OrderBookStateError("bid book is empty")
        return max(self._bids)

    @property
    def best_ask(self) -> Decimal:
        if not self._asks:
            raise OrderBookStateError("ask book is empty")
        return min(self._asks)

    @property
    def mid_price(self) -> Decimal:
        return (self.best_bid + self.best_ask) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal:
        mid = self.mid_price
        if mid <= 0:
            raise OrderBookStateError("mid price must be positive")
        return (self.best_ask - self.best_bid) / mid * Decimal("10000")

    def top_levels(
        self,
        depth: int = 10,
    ) -> tuple[tuple[OrderBookLevel, ...], tuple[OrderBookLevel, ...]]:
        if depth <= 0:
            raise ValueError("depth must be positive")
        bids = tuple(
            OrderBookLevel(price=price, size=self._bids[price])
            for price in sorted(self._bids, reverse=True)[:depth]
        )
        asks = tuple(
            OrderBookLevel(price=price, size=self._asks[price])
            for price in sorted(self._asks)[:depth]
        )
        return bids, asks

    def imbalance(self, depth: int = 10) -> Decimal:
        bids, asks = self.top_levels(depth)
        bid_size = sum((level.size for level in bids), Decimal("0"))
        ask_size = sum((level.size for level in asks), Decimal("0"))
        total = bid_size + ask_size
        if total == 0:
            raise OrderBookStateError("top-of-book liquidity is zero")
        return (bid_size - ask_size) / total
