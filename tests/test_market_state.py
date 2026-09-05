from decimal import Decimal

import pytest

from crypto_scanner.bybit.models import OrderBookLevel, OrderBookUpdate
from crypto_scanner.market_state import LocalOrderBook, OrderBookStateError


def _update(
    *,
    update_type: str,
    update_id: int,
    sequence: int,
    bids: tuple[tuple[str, str], ...],
    asks: tuple[tuple[str, str], ...],
) -> OrderBookUpdate:
    return OrderBookUpdate(
        symbol="BTCUSDT",
        update_type=update_type,
        timestamp_ms=1000 + update_id,
        engine_timestamp_ms=999 + update_id,
        update_id=update_id,
        sequence=sequence,
        bids=tuple(OrderBookLevel(Decimal(price), Decimal(size)) for price, size in bids),
        asks=tuple(OrderBookLevel(Decimal(price), Decimal(size)) for price, size in asks),
    )


def test_snapshot_initializes_book_and_delta_updates_levels() -> None:
    book = LocalOrderBook("BTCUSDT")
    book.apply(
        _update(
            update_type="snapshot",
            update_id=10,
            sequence=100,
            bids=(("100", "2"), ("99", "3")),
            asks=(("101", "4"), ("102", "5")),
        )
    )
    book.apply(
        _update(
            update_type="delta",
            update_id=11,
            sequence=101,
            bids=(("100", "0"), ("100.5", "1")),
            asks=(("101", "6"),),
        )
    )

    assert book.best_bid == Decimal("100.5")
    assert book.best_ask == Decimal("101")
    assert book.spread_bps > 0
    bids, asks = book.top_levels(2)
    assert bids[0].price == Decimal("100.5")
    assert asks[0].size == Decimal("6")


def test_delta_before_snapshot_fails_closed() -> None:
    book = LocalOrderBook("BTCUSDT")
    with pytest.raises(OrderBookStateError):
        book.apply(
            _update(
                update_type="delta",
                update_id=2,
                sequence=2,
                bids=(("100", "1"),),
                asks=(("101", "1"),),
            )
        )


def test_stale_update_id_fails_closed() -> None:
    book = LocalOrderBook("BTCUSDT")
    book.apply(
        _update(
            update_type="snapshot",
            update_id=10,
            sequence=100,
            bids=(("100", "1"),),
            asks=(("101", "1"),),
        )
    )
    with pytest.raises(OrderBookStateError):
        book.apply(
            _update(
                update_type="delta",
                update_id=10,
                sequence=101,
                bids=(("100", "2"),),
                asks=(),
            )
        )


def test_crossed_book_fails_closed() -> None:
    book = LocalOrderBook("BTCUSDT")
    with pytest.raises(OrderBookStateError):
        book.apply(
            _update(
                update_type="snapshot",
                update_id=10,
                sequence=100,
                bids=(("101", "1"),),
                asks=(("101", "1"),),
            )
        )


def test_imbalance_uses_actual_top_level_sizes() -> None:
    book = LocalOrderBook("BTCUSDT")
    book.apply(
        _update(
            update_type="snapshot",
            update_id=10,
            sequence=100,
            bids=(("100", "3"),),
            asks=(("101", "1"),),
        )
    )
    assert book.imbalance(depth=1) == Decimal("0.5")
