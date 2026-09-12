from __future__ import annotations

import json
import time as _stdlib_time
from collections.abc import Callable

from crypto_scanner import scanner_cycle
from crypto_scanner.hot_watch import FAST_WATCH_INTERVAL_SECONDS
from crypto_scanner.management_health import record_tick
from crypto_scanner.persistence import TransientPersistenceError
from crypto_scanner.profit_lock_watch import (
    ProfitLockWatchResult,
    emit_profit_lock_tick,
    run_profit_lock_tick,
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


def main() -> None:
    """Run the existing scanner cycle with serialized 1-minute position management."""
    original_time = scanner_cycle.time
    scanner_cycle.time = ProfitLockManagedClock()
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


if __name__ == "__main__":
    main()
