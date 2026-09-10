from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal

from crypto_scanner.binance.models import OrderSnapshot
from crypto_scanner.binance.private_rest import (
    BinanceDemoPrivateReadOnlyClient,
    BinancePrivateApiError,
)
from crypto_scanner.binance.private_write import (
    BinanceTestnetOrderClient,
    UnknownSubmissionOutcome,
    deterministic_management_id,
)
from crypto_scanner.position_manager_write import cleanup_scanner_orphans


class EmergencyExitError(RuntimeError):
    """Raised when a scanner-owned position cannot be proven flat."""


@dataclass(frozen=True, slots=True)
class EmergencyExitResult:
    symbol: str
    client_order_id: str
    exited_qty: Decimal
    venue_order_id: str


def _reconcile_exit(
    reader: BinanceDemoPrivateReadOnlyClient,
    *,
    symbol: str,
    client_order_id: str,
    exit_side: str,
    expected_qty: Decimal,
    attempts: int,
    sleep: Callable[[float], None],
) -> OrderSnapshot:
    last: OrderSnapshot | None = None
    for attempt in range(attempts):
        try:
            last = reader.get_order_by_client_id(symbol, client_order_id)
        except BinancePrivateApiError as exc:
            if "code=-2013" not in str(exc) or attempt + 1 >= attempts:
                raise
            sleep(0.25)
            continue
        if last.order_status == "FILLED":
            if (
                last.order_link_id != client_order_id
                or last.symbol != symbol
                or last.side != ("Buy" if exit_side == "BUY" else "Sell")
                or not last.reduce_only
                or (last.cum_exec_qty or Decimal(0)) < expected_qty
            ):
                raise EmergencyExitError(
                    "emergency exit reconciliation identity or quantity mismatch"
                )
            return last
        if last.order_status in {"CANCELED", "EXPIRED", "REJECTED"}:
            break
        if attempt + 1 < attempts:
            sleep(0.25)
    status = last.order_status if last is not None else "UNKNOWN"
    raise EmergencyExitError(f"emergency reduce-only exit did not fill: {status}")


def flatten_scanner_position(
    reader: BinanceDemoPrivateReadOnlyClient,
    writer: BinanceTestnetOrderClient,
    *,
    symbol: str,
    management_seed: str,
    attempts: int = 12,
    sleep: Callable[[float], None] = time.sleep,
) -> EmergencyExitResult:
    """Close one proven scanner position and verify the exchange is flat.

    The market exit is submitted once with a deterministic client id.  An unknown
    transport result is reconciled by that identity and is never blindly retried.
    Scanner-owned conditional orders are removed only after the flat state is proven.
    """
    if not 1 <= attempts <= 30:
        raise ValueError("emergency exit attempts must be between 1 and 30")
    positions = tuple(
        position
        for position in reader.get_positions()
        if position.symbol == symbol and position.is_open
    )
    if len(positions) != 1:
        raise EmergencyExitError(
            "emergency exit requires exactly one authoritative open position"
        )
    position = positions[0]
    if position.side not in {"Buy", "Sell"} or position.size <= 0:
        raise EmergencyExitError("emergency exit position is invalid")

    exit_side = "SELL" if position.side == "Buy" else "BUY"
    client_id = deterministic_management_id(symbol, management_seed, "panic")
    # Reconcile the exact identity below; never resubmit an unknown outcome.
    with suppress(UnknownSubmissionOutcome):
        writer.submit_reduce_only_market_exit(
            symbol=symbol,
            exit_side=exit_side,
            qty=position.size,
            client_order_id=client_id,
        )
    order = _reconcile_exit(
        reader,
        symbol=symbol,
        client_order_id=client_id,
        exit_side=exit_side,
        expected_qty=position.size,
        attempts=attempts,
        sleep=sleep,
    )
    remaining = ()
    for attempt in range(attempts):
        remaining = tuple(
            item
            for item in reader.get_positions()
            if item.symbol == symbol and item.is_open
        )
        if not remaining:
            break
        if attempt + 1 < attempts:
            sleep(0.25)
    if remaining:
        raise EmergencyExitError("emergency exit filled but position is not flat")
    cleanup_scanner_orphans(reader, writer, symbol)
    return EmergencyExitResult(
        symbol=symbol,
        client_order_id=client_id,
        exited_qty=position.size,
        venue_order_id=order.order_id,
    )
