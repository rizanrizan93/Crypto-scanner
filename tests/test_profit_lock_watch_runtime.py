from __future__ import annotations

import tomllib
from pathlib import Path

from crypto_scanner.hot_watch import FAST_WATCH_INTERVAL_SECONDS
from crypto_scanner.managed_scanner_cycle import ProfitLockManagedClock
from crypto_scanner.profit_lock_watch import ProfitLockWatchResult


def _result() -> ProfitLockWatchResult:
    return ProfitLockWatchResult(
        status="PASS_PROFIT_LOCK_WATCH",
        venue="BINANCE",
        environment="DEMO",
        live_trading_locked=True,
        decision_count=0,
        ratcheted_symbols=(),
        decisions=(),
    )


def test_fast_watch_interval_remains_exactly_one_minute() -> None:
    assert FAST_WATCH_INTERVAL_SECONDS == 60.0


def test_managed_clock_runs_profit_lock_after_fast_watch_sleep() -> None:
    events: list[tuple[str, object]] = []
    result = _result()
    clock = ProfitLockManagedClock(
        sleep_fn=lambda seconds: events.append(("sleep", seconds)),
        time_ns_fn=lambda: 123,
        tick_fn=lambda: events.append(("tick", None)) or result,
        emit_fn=lambda value: events.append(("emit", value)),
    )

    clock.sleep(FAST_WATCH_INTERVAL_SECONDS)

    assert events == [
        ("sleep", 60.0),
        ("tick", None),
        ("emit", result),
    ]
    assert clock.time_ns() == 123


def test_managed_clock_does_not_inject_tick_for_other_sleep_lengths() -> None:
    events: list[tuple[str, object]] = []
    clock = ProfitLockManagedClock(
        sleep_fn=lambda seconds: events.append(("sleep", seconds)),
        tick_fn=lambda: events.append(("tick", None)) or _result(),
        emit_fn=lambda value: events.append(("emit", value)),
    )

    clock.sleep(30.0)

    assert events == [("sleep", 30.0)]


def test_runtime_routes_scanner_through_managed_clock() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text())
    scripts = config["project"]["scripts"]

    assert scripts["crypto-scanner-cycle"] == "crypto_scanner.managed_scanner_cycle:main"
    assert scripts["crypto-scanner-profit-lock-watch"] == "crypto_scanner.profit_lock_watch:main"


def test_demo_runtime_fills_idle_window_with_serial_one_minute_ticks() -> None:
    workflow = Path(".github/workflows/demo-scanner-runtime.yml").read_text()

    assert "profit_lock_tick_seconds=60" in workflow
    assert "crypto-scanner-profit-lock-watch" in workflow
    assert 'while [ "${remaining}" -gt "${profit_lock_tick_seconds}" ]; do' in workflow
    assert 'sleep "${profit_lock_tick_seconds}"' in workflow
    assert "inputs.enable_demo_orders" in workflow
    execution_line = next(
        line
        for line in workflow.splitlines()
        if line.strip().startswith("CRYPTO_SCANNER_TESTNET_EXECUTION: ${{")
    )
    collection_line = next(
        line
        for line in workflow.splitlines()
        if line.strip().startswith("CRYPTO_SCANNER_DEMO_DATA_COLLECTION: ${{")
    )
    assert "workflow_dispatch" in execution_line
    assert "event_name == 'schedule'" in execution_line
    assert "workflow_dispatch" in collection_line
    assert "event_name == 'schedule'" in collection_line
    assert "Demo acquisition and calibration-data cycle" in workflow
