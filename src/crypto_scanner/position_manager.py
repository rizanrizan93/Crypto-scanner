from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from crypto_scanner.binance.models import PositionSnapshot
from crypto_scanner.binance.private_rest import AlgoOrderSnapshot
from crypto_scanner.lifecycle import AuthoritativeLifecycleSnapshot


class ProtectionStatus(StrEnum):
    PROTECTED = "PROTECTED"
    NO_POSITION = "NO_POSITION"
    MISSING_STOP = "MISSING_STOP"
    MISSING_TP = "MISSING_TP"
    WRONG_SIDE = "WRONG_SIDE"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    NOT_REDUCE_ONLY = "NOT_REDUCE_ONLY"
    DUPLICATE_PROTECTOR = "DUPLICATE_PROTECTOR"
    ORPHAN_PROTECTOR = "ORPHAN_PROTECTOR"


@dataclass(frozen=True, slots=True)
class ProtectionReport:
    symbol: str
    status: ProtectionStatus
    block_new_entries: bool
    detail: str
    stop: AlgoOrderSnapshot | None = None
    take_profit: AlgoOrderSnapshot | None = None


_ACTIVE_ALGO_STATUSES = frozenset({"NEW", "PENDING", "WORKING"})


def _active(orders: tuple[AlgoOrderSnapshot, ...]) -> tuple[AlgoOrderSnapshot, ...]:
    return tuple(order for order in orders if order.status.upper() in _ACTIVE_ALGO_STATUSES)


def _position_for_symbol(
    positions: tuple[PositionSnapshot, ...],
    symbol: str,
) -> PositionSnapshot | None:
    matches = tuple(position for position in positions if position.symbol == symbol and position.is_open)
    if len(matches) > 1:
        raise ValueError(f"multiple open position records for {symbol}; One-way Mode required")
    return matches[0] if matches else None


def audit_symbol_protection(
    symbol: str,
    positions: tuple[PositionSnapshot, ...],
    algo_orders: tuple[AlgoOrderSnapshot, ...],
) -> ProtectionReport:
    position = _position_for_symbol(positions, symbol)
    active = _active(tuple(order for order in algo_orders if order.symbol == symbol))
    if position is None:
        if active:
            return ProtectionReport(
                symbol=symbol,
                status=ProtectionStatus.ORPHAN_PROTECTOR,
                block_new_entries=True,
                detail="active reduce/conditional orders exist while exchange position is flat",
            )
        return ProtectionReport(
            symbol=symbol,
            status=ProtectionStatus.NO_POSITION,
            block_new_entries=False,
            detail="no open exchange position",
        )

    expected_side = "SELL" if position.side == "Buy" else "BUY"
    stops = tuple(order for order in active if order.order_type == "STOP_MARKET")
    tps = tuple(order for order in active if order.order_type == "TAKE_PROFIT_MARKET")
    if len(stops) > 1 or len(tps) > 1:
        return ProtectionReport(
            symbol=symbol,
            status=ProtectionStatus.DUPLICATE_PROTECTOR,
            block_new_entries=True,
            detail=f"active stops={len(stops)} active take-profits={len(tps)}",
        )
    stop = stops[0] if stops else None
    take_profit = tps[0] if tps else None
    if stop is None:
        return ProtectionReport(
            symbol=symbol,
            status=ProtectionStatus.MISSING_STOP,
            block_new_entries=True,
            detail="open exchange position has no active STOP_MARKET protector",
            take_profit=take_profit,
        )
    if take_profit is None:
        return ProtectionReport(
            symbol=symbol,
            status=ProtectionStatus.MISSING_TP,
            block_new_entries=True,
            detail="open exchange position has no active TAKE_PROFIT_MARKET protector",
            stop=stop,
        )
    if stop.side != expected_side or take_profit.side != expected_side:
        return ProtectionReport(
            symbol=symbol,
            status=ProtectionStatus.WRONG_SIDE,
            block_new_entries=True,
            detail=f"expected protector side={expected_side}",
            stop=stop,
            take_profit=take_profit,
        )
    if not stop.reduce_only or not take_profit.reduce_only:
        return ProtectionReport(
            symbol=symbol,
            status=ProtectionStatus.NOT_REDUCE_ONLY,
            block_new_entries=True,
            detail="all protectors must be reduceOnly=true",
            stop=stop,
            take_profit=take_profit,
        )
    if stop.quantity != position.size or take_profit.quantity != position.size:
        return ProtectionReport(
            symbol=symbol,
            status=ProtectionStatus.QUANTITY_MISMATCH,
            block_new_entries=True,
            detail=(
                f"position={position.size} stop={stop.quantity} tp={take_profit.quantity}"
            ),
            stop=stop,
            take_profit=take_profit,
        )
    return ProtectionReport(
        symbol=symbol,
        status=ProtectionStatus.PROTECTED,
        block_new_entries=False,
        detail="SL and TP2 are active, opposite-side, full-size, and reduce-only",
        stop=stop,
        take_profit=take_profit,
    )


def audit_all_protection(
    snapshot: AuthoritativeLifecycleSnapshot,
) -> tuple[ProtectionReport, ...]:
    symbols = {
        position.symbol for position in snapshot.open_positions
    } | {
        order.symbol
        for order in snapshot.open_algo_orders
        if order.status.upper() in _ACTIVE_ALGO_STATUSES
    }
    return tuple(
        audit_symbol_protection(symbol, snapshot.positions, snapshot.open_algo_orders)
        for symbol in sorted(symbols)
    )
