from __future__ import annotations

import re
import tomllib
from pathlib import Path


def _workflow(name: str) -> str:
    return Path(".github/workflows", name).read_text()


def _cron_fields(workflow: str) -> tuple[int, ...]:
    return tuple(
        len(value.split())
        for value in re.findall(r'cron:\s*["\u0027]([^"\u0027]+)["\u0027]', workflow)
    )


def test_demo_runtime_workflows_have_independent_concurrency_groups() -> None:
    workflows = tuple(
        _workflow(name)
        for name in (
            "demo-scanner-runtime.yml",
            "demo-trajectory-runtime.yml",
            "demo-calibration-runtime.yml",
            "demo-promotion-runtime.yml",
            "runtime-watchdog.yml",
            "demo-runtime-supervisor.yml",
        )
    )
    groups = tuple(
        next(
            line.split("group:", 1)[1].strip()
            for line in workflow.splitlines()
            if line.strip().startswith("group:")
        )
        for workflow in workflows
    )

    assert len(groups) == len(set(groups))
    assert all(group.startswith("crypto-scanner-demo-runtime-") for group in groups)
    assert all(fields == 5 for workflow in workflows for fields in _cron_fields(workflow))


def test_scanner_covers_full_hour_and_collects_demo_calibration_data() -> None:
    workflow = _workflow("demo-scanner-runtime.yml")

    assert "total_cycles=12" in workflow
    assert "total_cycles=11" not in workflow
    assert "Demo acquisition and calibration-data cycle" in workflow
    assert "CRYPTO_SCANNER_DEMO_DATA_COLLECTION" in workflow
    assert "HISTORICAL_PENDING/HISTORICAL_REJECTED" in workflow
    assert "QUARANTINED remains fail-closed" in workflow
    assert "FORWARD_DEMO promotion credit" in workflow
    assert "event_name == 'schedule'" in workflow
    assert "inputs.continuous_window" in workflow


def test_supervisor_self_heals_all_runtime_lanes_without_live_unlock() -> None:
    workflow = _workflow("demo-runtime-supervisor.yml")

    assert "actions: write" in workflow
    assert 'workflows:' in workflow
    assert '"Crypto Scanner Demo Runtime"' in workflow
    assert "demo-scanner-runtime.yml/dispatches" in workflow
    assert "inputs[enable_demo_orders]=true" in workflow
    assert "inputs[continuous_window]=true" in workflow
    assert "ensure_support_runtime TRAJECTORY_CYCLE" in workflow
    assert "ensure_support_runtime CALIBRATION_CYCLE" in workflow
    assert "ensure_support_runtime PROMOTION_CYCLE" in workflow
    assert "runtime-watchdog.yml" in workflow
    assert "live_trading_locked=1" in workflow


def test_promotion_and_watchdog_are_disarmed_and_heartbeat_backed() -> None:
    promotion = _workflow("demo-promotion-runtime.yml")
    watchdog = _workflow("runtime-watchdog.yml")

    assert "CRYPTO_SCANNER_TESTNET_EXECUTION: DISABLED" in promotion
    assert "crypto-scanner-strategy-promote" in promotion
    assert "PROMOTION_CYCLE RUNNING" in promotion
    assert "PROMOTION_CYCLE SUCCESS" in promotion
    assert "PROMOTION_CYCLE FAILED" in promotion
    assert "CRYPTO_SCANNER_TESTNET_EXECUTION: DISABLED" in watchdog
    assert "crypto-scanner-runtime-watchdog" in watchdog
    assert "WATCHDOG_CYCLE FAILED" in watchdog


def test_runtime_watchdog_console_script_is_installed() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text())

    assert config["project"]["scripts"]["crypto-scanner-runtime-watchdog"] == (
        "crypto_scanner.runtime_watchdog:main"
    )
