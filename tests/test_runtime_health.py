from __future__ import annotations

import json

import httpx
import pytest

from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig
from crypto_scanner.runtime_health import SupabaseRuntimeHealthStore


def _config() -> SupabasePersistenceConfig:
    return SupabasePersistenceConfig(
        url="https://abc.supabase.co",
        service_role_key="top-secret-key",
    )


def test_runtime_heartbeat_upserts_component_without_secret_leak() -> None:
    captured: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.url.path, request.read().decode()))
        return httpx.Response(201, json=[])

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        store = SupabaseRuntimeHealthStore(_config(), client=http_client)
        heartbeat = store.record(
            "scanner_cycle",
            "success",
            git_sha="abc123",
            details={"run_id": "42", "event": "schedule"},
            observed_at_ms=1234,
        )

    assert heartbeat.component == "SCANNER_CYCLE"
    assert heartbeat.status == "SUCCESS"
    assert captured[0][0] == "/rest/v1/heartbeats"
    payload = json.loads(captured[0][1])
    assert payload == [
        {
            "component": "SCANNER_CYCLE",
            "observed_at_ms": 1234,
            "status": "SUCCESS",
            "git_sha": "abc123",
            "details": {"run_id": "42", "event": "schedule"},
        }
    ]
    assert "top-secret-key" not in captured[0][1]


def test_runtime_heartbeat_rejects_invalid_status_before_network() -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(201, json=[])

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        store = SupabaseRuntimeHealthStore(_config(), client=http_client)
        with pytest.raises(PersistenceError, match="heartbeat status"):
            store.record("scanner", "UNKNOWN")

    assert not called


def test_runtime_heartbeat_rejects_unsafe_component() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(201))) as client:
        store = SupabaseRuntimeHealthStore(_config(), client=client)
        with pytest.raises(PersistenceError, match="component"):
            store.record("scanner/runtime", "SUCCESS")
