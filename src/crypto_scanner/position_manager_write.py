from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from crypto_scanner.binance.private_rest import (
    AlgoOrderSnapshot,
    BinanceDemoPrivateReadOnlyClient,
)
from crypto_scanner.binance.private_write import (
    BinanceTestnetOrderClient,
    ConditionalExitPlan,
    SplitProtectionPlan,
    UnknownSubmissionOutcome,
    deterministic_management_id,
)
from crypto_scanner.position_manager import ProtectionStatus, audit_symbol_protection


class PositionManagerError(RuntimeError):
    """Raised when an active position cannot be managed without violating safety."""


class ManagementStatus(StrEnum):
    PROTECTION_INSTALLED = "PROTECTION_INSTALLED"
    PROTECTION_RECONCILED = "PROTECTION_RECONCILED"
    ORPHANS_CLEANED = "ORPHANS_CLEANED"
    NO_ACTION = "NO_ACTION"


@dataclass(frozen=True, slots=True)
class ManagementResult:
    symbol: str
    status: ManagementStatus
    remaining_qty: Decimal
    submitted_ids: tuple[str, ...]
    cancelled_ids: tuple[str, ...]


_ACTIVE = frozenset({"NEW", "PENDING", "WORKING"})
_ALLOWED_PROTECTOR_TYPES = frozenset({"STOP_MARKET", "TAKE_PROFIT_MARKET"})
StageCallback = Callable[[str, str | None], None]


def _verify_active(
    reader: BinanceDemoPrivateReadOnlyClient,
    expected: ConditionalExitPlan,
) -> AlgoOrderSnapshot:
    actual = reader.get_algo_order_by_client_id(expected.client_algo_id)
    if actual.status.upper() not in _ACTIVE:
        raise PositionManagerError(
            f"protector {expected.client_algo_id} is not active: {actual.status}"
        )
    if actual.symbol != expected.symbol:
        raise PositionManagerError("protector reconciliation symbol mismatch")
    if actual.side != expected.exit_side:
        raise PositionManagerError("protector reconciliation side mismatch")
    if actual.order_type != expected.order_type:
        raise PositionManagerError("protector reconciliation type mismatch")
    if actual.quantity != expected.qty:
        raise PositionManagerError(
            f"protector quantity mismatch expected={expected.qty} actual={actual.quantity}"
        )
    if actual.trigger_price != expected.trigger_price:
        raise PositionManagerError(
            "protector trigger mismatch "
            f"expected={expected.trigger_price} actual={actual.trigger_price}"
        )
    if not actual.reduce_only:
        raise PositionManagerError("protector must be reduceOnly=true")
    return actual


def _matches(order: AlgoOrderSnapshot, expected: ConditionalExitPlan) -> bool:
    return (
        order.status.upper() in _ACTIVE
        and order.client_algo_id == expected.client_algo_id
        and order.symbol == expected.symbol
        and order.side == expected.exit_side
        and order.order_type == expected.order_type
        and order.quantity == expected.qty
        and order.trigger_price == expected.trigger_price
        and order.reduce_only
    )


def _active_for_symbol(
    reader: BinanceDemoPrivateReadOnlyClient,
    symbol: str,
) -> tuple[AlgoOrderSnapshot, ...]:
    return tuple(
        order
        for order in reader.get_open_algo_orders(symbol)
        if order.symbol == symbol and order.status.upper() in _ACTIVE
    )


def _scanner_owned_active(
    orders: tuple[AlgoOrderSnapshot, ...],
    symbol: str,
) -> tuple[AlgoOrderSnapshot, ...]:
    return tuple(
        order
        for order in orders
        if order.symbol == symbol
        and order.status.upper() in _ACTIVE
        and order.client_algo_id.startswith("cs-")
    )


def _assert_replaceable_existing(
    reader: BinanceDemoPrivateReadOnlyClient,
    symbol: str,
    exit_side: str,
) -> tuple[AlgoOrderSnapshot, ...]:
    active = _active_for_symbol(reader, symbol)
    for order in active:
        if (
            not order.client_algo_id.startswith("cs-")
            or not order.reduce_only
            or order.order_type not in _ALLOWED_PROTECTOR_TYPES
            or order.side != exit_side
        ):
            raise PositionManagerError(
                "aggregate replacement requires scanner-owned reduce-only protectors only"
            )
    return active


def _submit_or_reconcile_unknown(
    reader: BinanceDemoPrivateReadOnlyClient,
    writer: BinanceTestnetOrderClient,
    desired: ConditionalExitPlan,
) -> bool:
    """Submit once. Unknown transport outcome is accepted only if REST proves exact active state."""
    active = _active_for_symbol(reader, desired.symbol)
    if any(_matches(order, desired) for order in active):
        return False
    try:
        writer.submit_conditional_exit(desired)
    except UnknownSubmissionOutcome:
        reconciled = _active_for_symbol(reader, desired.symbol)
        if any(_matches(order, desired) for order in reconciled):
            return True
        raise
    _verify_active(reader, desired)
    return True


def _cancel_or_reconcile_unknown(
    reader: BinanceDemoPrivateReadOnlyClient,
    writer: BinanceTestnetOrderClient,
    *,
    symbol: str,
    client_algo_id: str,
) -> None:
    """Cancel once; an unknown result is safe only if REST proves the old order is absent."""
    try:
        writer.cancel_algo_order(symbol=symbol, client_algo_id=client_algo_id)
    except UnknownSubmissionOutcome:
        remaining = _active_for_symbol(reader, symbol)
        if any(order.client_algo_id == client_algo_id for order in remaining):
            raise
        return
    remaining = _active_for_symbol(reader, symbol)
    if any(order.client_algo_id == client_algo_id for order in remaining):
        raise PositionManagerError(
            f"protector cancellation did not reconcile: {client_algo_id}"
        )


def install_split_protection(
    reader: BinanceDemoPrivateReadOnlyClient,
    writer: BinanceTestnetOrderClient,
    plan: SplitProtectionPlan,
) -> ManagementResult:
    """Install stop first, then TP legs; reconcile every ACK before continuing."""
    submitted: list[str] = []

    writer.submit_conditional_exit(plan.stop_loss)
    _verify_active(reader, plan.stop_loss)
    submitted.append(plan.stop_loss.client_algo_id)

    if plan.take_profit_1 is not None:
        writer.submit_conditional_exit(plan.take_profit_1)
        _verify_active(reader, plan.take_profit_1)
        submitted.append(plan.take_profit_1.client_algo_id)

    writer.submit_conditional_exit(plan.take_profit_2)
    _verify_active(reader, plan.take_profit_2)
    submitted.append(plan.take_profit_2.client_algo_id)

    return ManagementResult(
        symbol=plan.stop_loss.symbol,
        status=ManagementStatus.PROTECTION_INSTALLED,
        remaining_qty=plan.full_qty,
        submitted_ids=tuple(submitted),
        cancelled_ids=(),
    )


def cleanup_scanner_orphans(
    reader: BinanceDemoPrivateReadOnlyClient,
    writer: BinanceTestnetOrderClient,
    symbol: str,
) -> ManagementResult:
    """Cancel only scanner-owned algo orders and only when Binance reports the symbol flat."""
    positions = tuple(
        position
        for position in reader.get_positions()
        if position.symbol == symbol and position.is_open
    )
    if positions:
        raise PositionManagerError("orphan cleanup forbidden while exchange position is open")

    active = _scanner_owned_active(reader.get_open_algo_orders(symbol), symbol)
    cancelled: list[str] = []
    for order in active:
        if not order.client_algo_id:
            raise PositionManagerError("scanner-owned orphan lacks deterministic clientAlgoId")
        _cancel_or_reconcile_unknown(
            reader,
            writer,
            symbol=symbol,
            client_algo_id=order.client_algo_id,
        )
        cancelled.append(order.client_algo_id)

    remaining = _scanner_owned_active(reader.get_open_algo_orders(symbol), symbol)
    if remaining:
        ids = ",".join(order.client_algo_id for order in remaining)
        raise PositionManagerError(f"scanner orphan cancellation did not reconcile: {ids}")

    return ManagementResult(
        symbol=symbol,
        status=(ManagementStatus.ORPHANS_CLEANED if cancelled else ManagementStatus.NO_ACTION),
        remaining_qty=Decimal(0),
        submitted_ids=(),
        cancelled_ids=tuple(cancelled),
    )


def replace_aggregate_protection(
    reader: BinanceDemoPrivateReadOnlyClient,
    writer: BinanceTestnetOrderClient,
    *,
    symbol: str,
    stop_trigger: Decimal,
    tp2_trigger: Decimal,
    management_seed: str,
    on_stage: StageCallback | None = None,
) -> ManagementResult:
    """Replace net-position protection without an intentional unprotected window.

    New full-size STOP is submitted/reconciled first, then new full-size TP2. Only
    after both are authoritatively active are stale scanner-owned protectors cancelled.
    Unknown writes are reconciled by deterministic ID and never blindly retried.
    """
    positions = tuple(
        position
        for position in reader.get_positions()
        if position.symbol == symbol and position.is_open
    )
    if len(positions) != 1:
        raise PositionManagerError(
            "aggregate protection replacement requires exactly one One-way net position"
        )
    position = positions[0]
    if position.side not in {"Buy", "Sell"} or position.size <= 0:
        raise PositionManagerError("authoritative aggregate position is invalid")
    if stop_trigger <= 0 or tp2_trigger <= 0:
        raise PositionManagerError("replacement triggers must be positive")

    exit_side = "SELL" if position.side == "Buy" else "BUY"
    existing = _assert_replaceable_existing(reader, symbol, exit_side)
    stop_id = deterministic_management_id(symbol, management_seed, "slr")
    tp2_id = deterministic_management_id(symbol, management_seed, "tp2r")
    desired_stop = ConditionalExitPlan(
        symbol=symbol,
        exit_side=exit_side,
        qty=position.size,
        trigger_price=stop_trigger,
        order_type="STOP_MARKET",
        client_algo_id=stop_id,
    )
    desired_tp2 = ConditionalExitPlan(
        symbol=symbol,
        exit_side=exit_side,
        qty=position.size,
        trigger_price=tp2_trigger,
        order_type="TAKE_PROFIT_MARKET",
        client_algo_id=tp2_id,
    )

    submitted: list[str] = []
    if _submit_or_reconcile_unknown(reader, writer, desired_stop):
        submitted.append(stop_id)
    if on_stage:
        on_stage("NEW_STOP_ACTIVE", stop_id)

    if _submit_or_reconcile_unknown(reader, writer, desired_tp2):
        submitted.append(tp2_id)
    if on_stage:
        on_stage("NEW_TP_ACTIVE", tp2_id)

    desired_ids = {stop_id, tp2_id}
    refreshed = _active_for_symbol(reader, symbol)
    if not any(_matches(order, desired_stop) for order in refreshed) or not any(
        _matches(order, desired_tp2) for order in refreshed
    ):
        raise PositionManagerError("replacement pair is not authoritatively active")
    if on_stage:
        on_stage("OLD_CANCEL_PENDING", None)

    cancelled: list[str] = []
    for order in refreshed:
        if order.client_algo_id in desired_ids:
            continue
        if order.client_algo_id.startswith("cs-") and order.order_type in _ALLOWED_PROTECTOR_TYPES:
            _cancel_or_reconcile_unknown(
                reader,
                writer,
                symbol=symbol,
                client_algo_id=order.client_algo_id,
            )
            cancelled.append(order.client_algo_id)

    final_positions = reader.get_positions()
    final_algos = reader.get_open_algo_orders(symbol)
    report = audit_symbol_protection(symbol, final_positions, final_algos)
    if report.status is not ProtectionStatus.PROTECTED:
        raise PositionManagerError(
            f"aggregate replacement final audit failed: {report.status.value}:{report.detail}"
        )
    if report.stop is None or report.take_profit is None:
        raise PositionManagerError("aggregate replacement final protectors are missing")
    if report.stop.client_algo_id != stop_id or report.take_profit.client_algo_id != tp2_id:
        raise PositionManagerError("aggregate replacement final protector identity mismatch")
    if on_stage:
        on_stage("PROTECTED", None)
    return ManagementResult(
        symbol=symbol,
        status=ManagementStatus.PROTECTION_RECONCILED,
        remaining_qty=position.size,
        submitted_ids=tuple(submitted),
        cancelled_ids=tuple(cancelled),
    )


def reconcile_remaining_protection(
    reader: BinanceDemoPrivateReadOnlyClient,
    writer: BinanceTestnetOrderClient,
    *,
    symbol: str,
    stop_trigger: Decimal,
    tp2_trigger: Decimal,
    management_seed: str,
) -> ManagementResult:
    """Resize protection after a partial exit using the same reconciled replacement protocol."""
    positions = tuple(
        position for position in reader.get_positions() if position.symbol == symbol and position.is_open
    )
    if not positions:
        return cleanup_scanner_orphans(reader, writer, symbol)
    if len(positions) != 1:
        raise PositionManagerError("One-way Mode requires exactly one open position record")

    position = positions[0]
    exit_side = "SELL" if position.side == "Buy" else "BUY"
    existing = _active_for_symbol(reader, symbol)
    exact_stop = next(
        (
            order
            for order in existing
            if order.order_type == "STOP_MARKET"
            and order.quantity == position.size
            and order.trigger_price == stop_trigger
            and order.side == exit_side
            and order.reduce_only
        ),
        None,
    )
    exact_tp2 = next(
        (
            order
            for order in existing
            if order.order_type == "TAKE_PROFIT_MARKET"
            and order.quantity == position.size
            and order.trigger_price == tp2_trigger
            and order.side == exit_side
            and order.reduce_only
        ),
        None,
    )
    if exact_stop is not None and exact_tp2 is not None and len(existing) == 2:
        return ManagementResult(
            symbol=symbol,
            status=ManagementStatus.NO_ACTION,
            remaining_qty=position.size,
            submitted_ids=(),
            cancelled_ids=(),
        )
    return replace_aggregate_protection(
        reader,
        writer,
        symbol=symbol,
        stop_trigger=stop_trigger,
        tp2_trigger=tp2_trigger,
        management_seed=management_seed,
    )
