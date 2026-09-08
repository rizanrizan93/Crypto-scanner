from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_scanner.algo_reconciliation import get_algo_order_eventually
from crypto_scanner.binance.private_rest import AlgoOrderSnapshot, BinancePrivateApiError


class DelayedReader:
    def __init__(self, failures: int, *, message: str = "Binance private API error code=-2013 msg=Order does not exist.") -> None:
        self.failures = failures
        self.message = message
        self.calls = 0

    def get_algo_order_by_client_id(self, client_algo_id: str) -> AlgoOrderSnapshot:
        self.calls += 1
        if self.calls <= self.failures:
            raise BinancePrivateApiError(self.message)
        return AlgoOrderSnapshot(
            algo_id="123",
            client_algo_id=client_algo_id,
            symbol="TRXUSDT",
            side="BUY",
            order_type="TAKE_PROFIT_MARKET",
            status="NEW",
            trigger_price=Decimal("0.25"),
            quantity=Decimal("39499"),
            reduce_only=True,
            updated_time_ms=1,
        )


def test_retries_only_transient_order_not_visible() -> None:
    reader = DelayedReader(2)
    sleeps: list[float] = []

    result = get_algo_order_eventually(
        reader,
        "cs-tp2-test",
        attempts=4,
        delay_seconds=0.1,
        sleep=sleeps.append,
    )

    assert result.status == "NEW"
    assert reader.calls == 3
    assert sleeps == [0.1, 0.1]


def test_non_transient_private_error_is_not_retried() -> None:
    reader = DelayedReader(1, message="Binance private API error code=-1022 msg=Signature invalid")

    with pytest.raises(BinancePrivateApiError, match="-1022"):
        get_algo_order_eventually(reader, "cs-tp2-test", attempts=5, sleep=lambda _: None)

    assert reader.calls == 1


def test_transient_error_still_fails_closed_after_bound() -> None:
    reader = DelayedReader(10)

    with pytest.raises(BinancePrivateApiError, match="-2013"):
        get_algo_order_eventually(reader, "cs-tp2-test", attempts=3, sleep=lambda _: None)

    assert reader.calls == 3
