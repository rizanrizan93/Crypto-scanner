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


def test_scanner_covers_full_hour_and_remains_promotion_gated() -> None:
    workflow = _workflow("demo-scanner-runtime.yml")

    assert "total_cycles=12" in workflow
    assert "total_cycles=11" not in workflow
    assert "promotion-gated Demo cycle" in workflow
    assert "event_name == 'schedule'" in workflow


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
