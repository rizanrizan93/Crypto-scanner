from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_scanner.algo_reconciliation import get_algo_order_eventually
from crypto_scanner.binance.private_rest import AlgoOrderSnapshot, BinancePrivateApiError


def _order(client_algo_id: str) -> AlgoOrderSnapshot:
    return AlgoOrderSnapshot(
        algo_id="algo-123",
        client_algo_id=client_algo_id,
        symbol="DOGEUSDT",
        side="BUY",
        order_type="TAKE_PROFIT_MARKET",
        status="NEW",
        trigger_price=Decimal("0.08154"),
        quantity=Decimal("144141"),
        reduce_only=True,
        updated_time_ms=1,
    )


class CollectionVisibleBeforeLookupReader:
    def __init__(self, *, collection_visible: bool) -> None:
        self.collection_visible = collection_visible
        self.lookup_calls = 0
        self.collection_calls = 0

    def get_algo_order_by_client_id(self, client_algo_id: str) -> AlgoOrderSnapshot:
        self.lookup_calls += 1
        raise BinancePrivateApiError(
            "Binance private API error code=-2013 msg=Order does not exist."
        )

    def get_open_algo_orders(self, symbol: str | None = None) -> tuple[AlgoOrderSnapshot, ...]:
        self.collection_calls += 1
        if not self.collection_visible:
            return ()
        return (_order("cs-tp2r-race"),)


def test_open_collection_reconciles_post_success_single_lookup_race() -> None:
    reader = CollectionVisibleBeforeLookupReader(collection_visible=True)
    sleeps: list[float] = []

    result = get_algo_order_eventually(
        reader,
        "cs-tp2r-race",
        attempts=4,
        delay_seconds=0.1,
        sleep=sleeps.append,
    )

    assert result.client_algo_id == "cs-tp2r-race"
    assert result.status == "NEW"
    assert reader.lookup_calls == 1
    assert reader.collection_calls == 1
    assert sleeps == []


def test_unresolved_minus_2013_remains_fail_closed_without_inventing_success() -> None:
    reader = CollectionVisibleBeforeLookupReader(collection_visible=False)

    with pytest.raises(BinancePrivateApiError, match="-2013"):
        get_algo_order_eventually(
            reader,
            "cs-tp2r-race",
            attempts=3,
            delay_seconds=0,
            sleep=lambda _: None,
        )

    assert reader.lookup_calls == 3
    # one collection check per transient miss plus the final boundary check
    assert reader.collection_calls == 4
