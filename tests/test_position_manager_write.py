from decimal import Decimal

import pytest

from crypto_scanner.binance.models import PositionSnapshot
from crypto_scanner.binance.private_rest import AlgoOrderSnapshot
from crypto_scanner.position_manager_write import (
    ManagementStatus,
    PositionManagerError,
    cleanup_scanner_orphans,
    reconcile_remaining_protection,
)


def _position(qty: str = "2") -> PositionSnapshot:
    return PositionSnapshot(
        symbol="XRPUSDT",
        side="Buy",
        size=Decimal(qty),
        avg_price=Decimal("1.50"),
        position_value=Decimal(qty) * Decimal("1.50"),
        leverage=Decimal("1"),
        mark_price=Decimal("1.55"),
        liq_price=None,
        unrealised_pnl=Decimal("0"),
        cum_realised_pnl=None,
        position_im=None,
        position_mm=None,
        take_profit=None,
        stop_loss=None,
        trailing_stop=None,
        updated_time_ms=1000,
    )


def _algo(
    client_id: str,
    order_type: str,
    qty: str,
    trigger: str,
) -> AlgoOrderSnapshot:
    return AlgoOrderSnapshot(
        algo_id=f"algo-{client_id}",
        client_algo_id=client_id,
        symbol="XRPUSDT",
        side="SELL",
        order_type=order_type,
        status="NEW",
        trigger_price=Decimal(trigger),
        quantity=Decimal(qty),
        reduce_only=True,
        updated_time_ms=1000,
    )


class FakeReader:
    def __init__(
        self,
        positions: tuple[PositionSnapshot, ...],
        orders: list[AlgoOrderSnapshot],
    ) -> None:
        self.positions = positions
        self.orders = orders

    def get_positions(self) -> tuple[PositionSnapshot, ...]:
        return self.positions

    def get_open_algo_orders(self, symbol: str | None = None) -> tuple[AlgoOrderSnapshot, ...]:
        if symbol is None:
            return tuple(self.orders)
        return tuple(order for order in self.orders if order.symbol == symbol)

    def get_algo_order_by_client_id(self, client_id: str) -> AlgoOrderSnapshot:
        return next(order for order in self.orders if order.client_algo_id == client_id)


class FakeWriter:
    def __init__(self, reader: FakeReader) -> None:
        self.reader = reader
        self.cancelled: list[str] = []
        self.submitted: list[str] = []

    def cancel_algo_order(self, *, symbol: str, client_algo_id: str) -> object:
        self.cancelled.append(client_algo_id)
        self.reader.orders = [
            order
            for order in self.reader.orders
            if not (order.symbol == symbol and order.client_algo_id == client_algo_id)
        ]
        return object()

    def submit_conditional_exit(self, plan: object) -> object:
        self.submitted.append(plan.client_algo_id)
        self.reader.orders.append(
            AlgoOrderSnapshot(
                algo_id=f"new-{plan.client_algo_id}",
                client_algo_id=plan.client_algo_id,
                symbol=plan.symbol,
                side=plan.exit_side,
                order_type=plan.order_type,
                status="NEW",
                trigger_price=plan.trigger_price,
                quantity=plan.qty,
                reduce_only=True,
                updated_time_ms=2000,
            )
        )
        return object()


def test_orphan_cleanup_never_touches_manual_order() -> None:
    reader = FakeReader(
        (),
        [
            _algo("cs-sl-old", "STOP_MARKET", "2", "1.40"),
            _algo("manual-protection", "STOP_MARKET", "2", "1.30"),
        ],
    )
    writer = FakeWriter(reader)
    result = cleanup_scanner_orphans(reader, writer, "XRPUSDT")
    assert result.status is ManagementStatus.ORPHANS_CLEANED
    assert writer.cancelled == ["cs-sl-old"]
    assert [order.client_algo_id for order in reader.orders] == ["manual-protection"]


def test_orphan_cleanup_refuses_open_position() -> None:
    reader = FakeReader((_position(),), [_algo("cs-sl-old", "STOP_MARKET", "2", "1.40")])
    with pytest.raises(PositionManagerError, match="position is open"):
        cleanup_scanner_orphans(reader, FakeWriter(reader), "XRPUSDT")


def test_partial_exit_replaces_wrong_size_protectors_before_cancel() -> None:
    reader = FakeReader(
        (_position("2"),),
        [
            _algo("cs-sl-old", "STOP_MARKET", "4", "1.40"),
            _algo("cs-tp2-old", "TAKE_PROFIT_MARKET", "4", "2.00"),
        ],
    )
    writer = FakeWriter(reader)
    result = reconcile_remaining_protection(
        reader,
        writer,
        symbol="XRPUSDT",
        stop_trigger=Decimal("1.40"),
        tp2_trigger=Decimal("2.00"),
        management_seed="partial-fill-77",
    )
    assert result.status is ManagementStatus.PROTECTION_RECONCILED
    assert result.remaining_qty == Decimal("2")
    assert len(writer.submitted) == 2
    assert set(writer.cancelled) == {"cs-sl-old", "cs-tp2-old"}
    current = [order for order in reader.orders if order.client_algo_id.startswith("cs-")]
    assert len(current) == 2
    assert all(order.quantity == Decimal("2") for order in current)


def test_exact_remaining_protection_needs_no_write() -> None:
    reader = FakeReader(
        (_position("2"),),
        [
            _algo("cs-sl-existing", "STOP_MARKET", "2", "1.40"),
            _algo("cs-tp-existing", "TAKE_PROFIT_MARKET", "2", "2.00"),
        ],
    )
    writer = FakeWriter(reader)
    result = reconcile_remaining_protection(
        reader,
        writer,
        symbol="XRPUSDT",
        stop_trigger=Decimal("1.40"),
        tp2_trigger=Decimal("2.00"),
        management_seed="unused",
    )
    assert result.status is ManagementStatus.NO_ACTION
    assert writer.submitted == []
    assert writer.cancelled == []
