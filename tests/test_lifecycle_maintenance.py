from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_scanner.binance.models import PositionSnapshot
from crypto_scanner.binance.private_rest import AlgoOrderSnapshot
from crypto_scanner.binance.private_write import UnknownSubmissionOutcome
from crypto_scanner.lifecycle_maintenance import run_lifecycle_maintenance


def _position(symbol: str = "XRPUSDT") -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        side="Buy",
        size=Decimal("3.6"),
        avg_price=Decimal("1.415"),
        position_value=Decimal("5.094"),
        leverage=Decimal("1"),
        mark_price=Decimal("1.41"),
        liq_price=None,
        unrealised_pnl=Decimal("0"),
        cum_realised_pnl=None,
        position_im=None,
        position_mm=None,
        take_profit=None,
        stop_loss=None,
        trailing_stop=None,
        updated_time_ms=1,
    )


def _algo(
    client_id: str = "cs-tp2-0123456789abcdef",
    *,
    symbol: str = "XRPUSDT",
    reduce_only: bool = True,
    order_type: str = "TAKE_PROFIT_MARKET",
) -> AlgoOrderSnapshot:
    return AlgoOrderSnapshot(
        algo_id="42",
        client_algo_id=client_id,
        symbol=symbol,
        side="SELL",
        order_type=order_type,
        status="NEW",
        trigger_price=Decimal("1.44"),
        quantity=Decimal("3.6"),
        reduce_only=reduce_only,
        updated_time_ms=1,
    )


class FakeReader:
    def __init__(
        self,
        *,
        positions: tuple[PositionSnapshot, ...] = (),
        orders: tuple[AlgoOrderSnapshot, ...] = (),
    ) -> None:
        self.positions = list(positions)
        self.orders = list(orders)

    def get_positions(self) -> tuple[PositionSnapshot, ...]:
        return tuple(self.positions)

    def get_open_algo_orders(self, symbol: str | None = None) -> tuple[AlgoOrderSnapshot, ...]:
        if symbol is None:
            return tuple(self.orders)
        return tuple(order for order in self.orders if order.symbol == symbol)


class FakeWriter:
    def __init__(self, reader: FakeReader, *, unknown: bool = False) -> None:
        self.reader = reader
        self.unknown = unknown
        self.calls: list[str] = []

    def cancel_algo_order(self, *, symbol: str, client_algo_id: str) -> object:
        self.calls.append(client_algo_id)
        if self.unknown:
            raise UnknownSubmissionOutcome(client_algo_id, "unknown cancellation outcome")
        self.reader.orders = [
            order
            for order in self.reader.orders
            if not (order.symbol == symbol and order.client_algo_id == client_algo_id)
        ]
        return object()


def test_flat_scanner_owned_orphan_is_cancelled_and_reconciled() -> None:
    reader = FakeReader(orders=(_algo(),))
    writer = FakeWriter(reader)

    result = run_lifecycle_maintenance(reader, writer)

    assert result.status == "PASS_LIFECYCLE_MAINTENANCE"
    assert result.cleaned_symbols == ("XRPUSDT",)
    assert result.cancelled_ids == ("cs-tp2-0123456789abcdef",)
    assert result.blockers == ()
    assert reader.orders == []


def test_manual_flat_orphan_blocks_without_any_cancellation() -> None:
    reader = FakeReader(orders=(_algo("manual-order-1"),))
    writer = FakeWriter(reader)

    result = run_lifecycle_maintenance(reader, writer)

    assert result.status == "BLOCKED_LIFECYCLE_MAINTENANCE"
    assert "UNSAFE_FLAT_ORPHAN:XRPUSDT" in result.blockers
    assert writer.calls == []
    assert len(reader.orders) == 1


def test_mixed_manual_and_scanner_orphans_leave_both_untouched() -> None:
    reader = FakeReader(orders=(_algo(), _algo("manual-order-1")))
    writer = FakeWriter(reader)

    result = run_lifecycle_maintenance(reader, writer)

    assert result.status == "BLOCKED_LIFECYCLE_MAINTENANCE"
    assert writer.calls == []
    assert len(reader.orders) == 2


def test_open_position_protector_is_never_cancelled_by_orphan_maintenance() -> None:
    reader = FakeReader(positions=(_position(),), orders=(_algo(),))
    writer = FakeWriter(reader)

    result = run_lifecycle_maintenance(reader, writer)

    assert result.status == "PASS_LIFECYCLE_MAINTENANCE"
    assert result.open_position_symbols == ("XRPUSDT",)
    assert result.cleaned_symbols == ()
    assert writer.calls == []
    assert len(reader.orders) == 1


def test_malformed_scanner_orphan_fails_closed_without_cancel() -> None:
    reader = FakeReader(orders=(_algo(reduce_only=False),))
    writer = FakeWriter(reader)

    result = run_lifecycle_maintenance(reader, writer)

    assert result.status == "BLOCKED_LIFECYCLE_MAINTENANCE"
    assert writer.calls == []


def test_unknown_cancel_outcome_is_not_retried() -> None:
    reader = FakeReader(orders=(_algo(),))
    writer = FakeWriter(reader, unknown=True)

    with pytest.raises(UnknownSubmissionOutcome):
        run_lifecycle_maintenance(reader, writer)

    assert writer.calls == ["cs-tp2-0123456789abcdef"]
