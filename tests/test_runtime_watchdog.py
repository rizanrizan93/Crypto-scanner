from __future__ import annotations

import httpx
import pytest

from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig
from crypto_scanner.runtime_watchdog import (
    DEFAULT_POLICIES,
    SupabaseRuntimeWatchdogStore,
    evaluate_runtime_health,
)


def _rows(now_ms: int) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "component": policy.component,
            "observed_at_ms": now_ms - 1_000,
            "status": "SUCCESS",
        }
        for policy in DEFAULT_POLICIES
    )


def test_runtime_watchdog_accepts_all_fresh_healthy_components() -> None:
    report = evaluate_runtime_health(_rows(100_000), now_ms=100_000)

    assert report.healthy
    assert all(component.reason == "HEALTHY" for component in report.components)
    assert report.to_dict()["live_trading_locked"] is True


@pytest.mark.parametrize("status", ["FAILED", "BLOCKED"])
def test_runtime_watchdog_fails_closed_on_bad_status(status: str) -> None:
    rows = list(_rows(100_000))
    rows[0] = {**rows[0], "status": status}

    report = evaluate_runtime_health(tuple(rows), now_ms=100_000)

    assert not report.healthy
    assert report.components[0].reason == f"UNHEALTHY_STATUS_{status}"


def test_runtime_watchdog_detects_missing_and_stale_components() -> None:
    scanner_policy = DEFAULT_POLICIES[0]
    stale_at = 100_000 - scanner_policy.max_age_seconds * 1_000 - 1
    report = evaluate_runtime_health(
        ({"component": "SCANNER_CYCLE", "observed_at_ms": stale_at, "status": "RUNNING"},),
        now_ms=100_000,
    )

    assert not report.healthy
    assert report.components[0].reason == "STALE_HEARTBEAT"
    assert all(item.reason == "MISSING_HEARTBEAT" for item in report.components[1:])


def test_runtime_watchdog_rest_query_is_bounded_to_required_components() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    config = SupabasePersistenceConfig(
        url="https://abc.supabase.co",
        service_role_key="secret",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        store = SupabaseRuntimeWatchdogStore(config, client=client)
        assert store.read_components(tuple(item.component for item in DEFAULT_POLICIES)) == ()

    assert len(requests) == 1
    assert requests[0].url.path == "/rest/v1/heartbeats"
    assert requests[0].url.params["select"] == "component,observed_at_ms,status"
    assert requests[0].url.params["component"].startswith("in.(SCANNER_CYCLE,")


def test_runtime_watchdog_rejects_malformed_rows() -> None:
    with pytest.raises(PersistenceError, match="malformed"):
        evaluate_runtime_health(({"component": "SCANNER_CYCLE"},), now_ms=1)
