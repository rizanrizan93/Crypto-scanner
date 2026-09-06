from __future__ import annotations

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
    deterministic_management_id,
)


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
        writer.cancel_algo_order(symbol=symbol, client_algo_id=order.client_algo_id)
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


def reconcile_remaining_protection(
    reader: BinanceDemoPrivateReadOnlyClient,
    writer: BinanceTestnetOrderClient,
    *,
    symbol: str,
    stop_trigger: Decimal,
    tp2_trigger: Decimal,
    management_seed: str,
) -> ManagementResult:
    """After a partial exit, resize SL and final TP to the authoritative remaining quantity.

    Replacement is submit-and-verify before old scanner-owned protection is cancelled, so the
    position is never intentionally left without a server-side stop.
    """
    positions = tuple(
        position
        for position in reader.get_positions()
        if position.symbol == symbol and position.is_open
    )
    if not positions:
        return cleanup_scanner_orphans(reader, writer, symbol)
    if len(positions) != 1:
        raise PositionManagerError("One-way Mode requires exactly one open position record")
    position = positions[0]
    if position.side not in {"Buy", "Sell"} or position.size <= 0:
        raise PositionManagerError("authoritative remaining position is invalid")
    if stop_trigger <= 0 or tp2_trigger <= 0:
        raise PositionManagerError("replacement triggers must be positive")

    exit_side = "SELL" if position.side == "Buy" else "BUY"
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

    existing = _scanner_owned_active(reader.get_open_algo_orders(symbol), symbol)
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
    if exact_stop is not None and exact_tp2 is not None:
        return ManagementResult(
            symbol=symbol,
            status=ManagementStatus.NO_ACTION,
            remaining_qty=position.size,
            submitted_ids=(),
            cancelled_ids=(),
        )

    submitted: list[str] = []
    if exact_stop is None:
        writer.submit_conditional_exit(desired_stop)
        _verify_active(reader, desired_stop)
        submitted.append(stop_id)
    if exact_tp2 is None:
        writer.submit_conditional_exit(desired_tp2)
        _verify_active(reader, desired_tp2)
        submitted.append(tp2_id)

    protected_ids = {
        (exact_stop.client_algo_id if exact_stop else stop_id),
        (exact_tp2.client_algo_id if exact_tp2 else tp2_id),
    }
    cancelled: list[str] = []
    refreshed = _scanner_owned_active(reader.get_open_algo_orders(symbol), symbol)
    for order in refreshed:
        if order.client_algo_id in protected_ids:
            continue
        if order.order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            continue
        writer.cancel_algo_order(symbol=symbol, client_algo_id=order.client_algo_id)
        cancelled.append(order.client_algo_id)

    final_orders = _scanner_owned_active(reader.get_open_algo_orders(symbol), symbol)
    final_stop = tuple(order for order in final_orders if order.client_algo_id in protected_ids)
    if len(final_stop) != 2:
        raise PositionManagerError("replacement protection did not reconcile to exactly two legs")

    return ManagementResult(
        symbol=symbol,
        status=ManagementStatus.PROTECTION_RECONCILED,
        remaining_qty=position.size,
        submitted_ids=tuple(submitted),
        cancelled_ids=tuple(cancelled),
    )
