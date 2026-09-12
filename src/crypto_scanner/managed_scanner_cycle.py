from __future__ import annotations

import json
import os
import time as _stdlib_time
from collections.abc import Callable
from dataclasses import replace

from crypto_scanner import scanner_cycle
from crypto_scanner.hot_watch import FAST_WATCH_INTERVAL_SECONDS
from crypto_scanner.management_health import record_tick
from crypto_scanner.persistence import TransientPersistenceError
from crypto_scanner.profit_lock_watch import (
    ProfitLockWatchResult,
    emit_profit_lock_tick,
    run_profit_lock_tick,
)
from crypto_scanner.strategy_promotion import (
    PromotionStage,
    StrategyRuntimeSelection,
    load_strategy_runtime,
)

_DEMO_COLLECTION_ENV = "CRYPTO_SCANNER_DEMO_DATA_COLLECTION"
_DEMO_COLLECTION_STAGES = frozenset(
    {
        "UNVALIDATED",
        PromotionStage.HISTORICAL_PENDING.value,
        PromotionStage.HISTORICAL_REJECTED.value,
    }
)


class ProfitLockManagedClock:
    """Proxy scanner-cycle time calls and serialize a profit-lock tick after 60s sleeps."""

    def __init__(
        self,
        *,
        sleep_fn: Callable[[float], None] = _stdlib_time.sleep,
        time_ns_fn: Callable[[], int] = _stdlib_time.time_ns,
        tick_fn: Callable[[], ProfitLockWatchResult] = run_profit_lock_tick,
        emit_fn: Callable[[ProfitLockWatchResult], None] = emit_profit_lock_tick,
    ) -> None:
        self._sleep_fn = sleep_fn
        self._time_ns_fn = time_ns_fn
        self._tick_fn = tick_fn
        self._emit_fn = emit_fn

    def time_ns(self) -> int:
        return self._time_ns_fn()

    def sleep(self, seconds: float) -> None:
        self._sleep_fn(seconds)
        if seconds == FAST_WATCH_INTERVAL_SECONDS:
            result = self._tick_fn()
            self._emit_fn(result)


def demo_data_collection_enabled() -> bool:
    return os.getenv(_DEMO_COLLECTION_ENV, "DISABLED").strip().upper() == "ENABLED"


def authorize_demo_data_collection(
    runtime: StrategyRuntimeSelection,
    *,
    enabled: bool,
) -> StrategyRuntimeSelection:
    """Allow Demo-only acquisition for research states without promoting them.

    The returned runtime may be executable by scanner_cycle, but its promotion_stage
    remains unchanged. QUARANTINED and all other non-research states remain fail-closed.
    The Binance writer still independently requires the TestnetExecutionArm.
    """

    if not enabled or runtime.execution_authorized:
        return runtime
    if runtime.promotion_stage not in _DEMO_COLLECTION_STAGES:
        return runtime
    return replace(runtime, execution_authorized=True)


def _load_managed_strategy_runtime(config: object) -> StrategyRuntimeSelection:
    runtime = load_strategy_runtime(config)  # type: ignore[arg-type]
    return authorize_demo_data_collection(
        runtime,
        enabled=demo_data_collection_enabled(),
    )


def main() -> None:
    """Run the scanner with serialized management and optional Demo data collection."""
    original_time = scanner_cycle.time
    original_loader = scanner_cycle.load_strategy_runtime
    collection_enabled = demo_data_collection_enabled()
    scanner_cycle.time = ProfitLockManagedClock()
    scanner_cycle.load_strategy_runtime = _load_managed_strategy_runtime
    if collection_enabled:
        print(
            json.dumps(
                {
                    "status": "DEMO_DATA_COLLECTION_MODE_ENABLED",
                    "promotion_bypass": False,
                    "forward_demo_credit": False,
                    "live_trading_locked": True,
                },
                sort_keys=True,
            )
        )
    try:
        scanner_cycle.main()
    except TransientPersistenceError:
        # Only the typed READ exhaustion is handled. Unknown write outcomes,
        # integrity errors and protection violations remain hard failures.
        health = record_tick(degraded=True)
        print(
            json.dumps(
                {
                    "status": "DEGRADED_PERSISTENCE_TRANSIENT",
                    "execution_authorized": False,
                    "management_heartbeat": health,
                    "live_trading_locked": True,
                }
            )
        )
    finally:
        scanner_cycle.time = original_time
        scanner_cycle.load_strategy_runtime = original_loader


if __name__ == "__main__":
    main()
