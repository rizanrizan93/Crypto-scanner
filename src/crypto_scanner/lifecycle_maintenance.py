from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_rest import (
    AlgoOrderSnapshot,
    BinanceDemoPrivateReadOnlyClient,
)
from crypto_scanner.binance.private_write import BinanceTestnetOrderClient
from crypto_scanner.config import load_runtime_config
from crypto_scanner.execution_plan import TestnetExecutionArm
from crypto_scanner.position_manager_write import cleanup_scanner_orphans
from crypto_scanner.safety import SafetyContract

_ACTIVE_ALGO_STATUSES = frozenset({"NEW", "PENDING", "WORKING"})
_ALLOWED_ORPHAN_TYPES = frozenset({"STOP_MARKET", "TAKE_PROFIT_MARKET"})


class LifecycleMaintenanceError(RuntimeError):
    """Raised when flat-account maintenance cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class LifecycleMaintenanceResult:
    status: str
    venue: str
    environment: str
    live_trading_locked: bool
    open_position_symbols: tuple[str, ...]
    cleaned_symbols: tuple[str, ...]
    cancelled_ids: tuple[str, ...]
    blockers: tuple[str, ...]


def _active_orders(
    orders: tuple[AlgoOrderSnapshot, ...],
) -> tuple[AlgoOrderSnapshot, ...]:
    return tuple(
        order for order in orders if order.status.upper() in _ACTIVE_ALGO_STATUSES
    )


def run_lifecycle_maintenance(
    reader: BinanceDemoPrivateReadOnlyClient,
    writer: BinanceTestnetOrderClient,
    *,
    safety: SafetyContract | None = None,
) -> LifecycleMaintenanceResult:
    """Clean only scanner-owned conditional siblings left after a symbol becomes flat.

    Manual/non-scanner algo orders are never cancelled. If a flat symbol contains any
    non-scanner order, malformed scanner order, or unsupported order type, maintenance
    fails closed and leaves every order on that symbol untouched.
    """
    safety = safety or SafetyContract()
    safety.validate()

    open_positions = tuple(position for position in reader.get_positions() if position.is_open)
    open_position_symbols = tuple(sorted(position.symbol for position in open_positions))
    open_symbol_set = set(open_position_symbols)
    active = _active_orders(reader.get_open_algo_orders())

    cleaned_symbols: list[str] = []
    cancelled_ids: list[str] = []
    blockers: list[str] = []

    for symbol in sorted({order.symbol for order in active}):
        if symbol in open_symbol_set:
            continue
        symbol_orders = tuple(order for order in active if order.symbol == symbol)
        invalid = tuple(
            order
            for order in symbol_orders
            if not order.client_algo_id.startswith("cs-")
            or not order.reduce_only
            or order.order_type not in _ALLOWED_ORPHAN_TYPES
        )
        if invalid:
            blockers.append(f"UNSAFE_FLAT_ORPHAN:{symbol}")
            continue

        result = cleanup_scanner_orphans(reader, writer, symbol)
        if result.cancelled_ids:
            cleaned_symbols.append(symbol)
            cancelled_ids.extend(result.cancelled_ids)

    post_positions = tuple(position for position in reader.get_positions() if position.is_open)
    post_position_symbols = {position.symbol for position in post_positions}
    post_active = _active_orders(reader.get_open_algo_orders())
    for symbol in sorted({order.symbol for order in post_active} - post_position_symbols):
        marker = f"FLAT_ORPHAN_REMAINS:{symbol}"
        if marker not in blockers:
            blockers.append(marker)

    status = "PASS_LIFECYCLE_MAINTENANCE" if not blockers else "BLOCKED_LIFECYCLE_MAINTENANCE"
    return LifecycleMaintenanceResult(
        status=status,
        venue="BINANCE",
        environment="DEMO",
        live_trading_locked=safety.live_trading_locked,
        open_position_symbols=open_position_symbols,
        cleaned_symbols=tuple(cleaned_symbols),
        cancelled_ids=tuple(cancelled_ids),
        blockers=tuple(blockers),
    )


def main() -> None:
    safety = SafetyContract()
    safety.validate()
    arm = TestnetExecutionArm.from_environment()
    arm.require_enabled()
    config = load_runtime_config()
    credentials = BinanceDemoCredentials.from_environment()

    with (
        BinanceDemoPrivateReadOnlyClient(
            credentials,
            base_url=config.binance_rest_url,
        ) as reader,
        BinanceTestnetOrderClient(
            credentials,
            arm,
            base_url=config.binance_rest_url,
        ) as writer,
    ):
        result = run_lifecycle_maintenance(reader, writer, safety=safety)

    print(json.dumps(asdict(result), indent=2, sort_keys=True))
    if result.blockers:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
