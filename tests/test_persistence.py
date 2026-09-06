from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from crypto_scanner.closed_trades import TradeDirection
from crypto_scanner.persistence import (
    SCHEMA_VERSION,
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseTrajectoryStore,
    stable_position_id,
)
from crypto_scanner.trajectory import TrajectoryMetrics, TrajectoryQuality
from crypto_scanner.trajectory_store import TrajectoryRecord, TrajectoryState


def _record(state: TrajectoryState = TrajectoryState.OPEN) -> TrajectoryRecord:
    metrics = TrajectoryMetrics(
        symbol="XRPUSDT",
        direction=TradeDirection.LONG,
        entry_time_ms=1_000_000,
        measured_until_ms=1_300_000,
        entry_price=Decimal("1.415"),
        current_price=Decimal("1.430"),
        reference_qty=Decimal("3.6"),
        favorable_extreme_price=Decimal("1.440"),
        adverse_extreme_price=Decimal("1.410"),
        mfe_per_unit=Decimal("0.025"),
        mae_per_unit=Decimal("0.005"),
        mfe_pct=Decimal("1.766784452296819787985865724"),
        mae_pct=Decimal("0.3533568904593639575971731449"),
        current_pnl_per_unit=Decimal("0.015"),
        observation_count=5,
        holding_time_ms=300_000,
        quality=TrajectoryQuality.CONSERVATIVE_1M_REPLAY,
        history_complete=True,
    )
    if state is TrajectoryState.CLOSED:
        return TrajectoryRecord(
            snapshot=metrics,
            state=state,
            calibration_eligible=False,
            persistence_mode="SUPABASE",
            note="closed",
            realized_pnl=Decimal("0.04"),
            commission=Decimal("0.001"),
            funding_fee=Decimal("-0.0002"),
            net_pnl=Decimal("0.0388"),
            exit_time_ms=1_300_000,
            exit_price=Decimal("1.430"),
        )
    return TrajectoryRecord(
        snapshot=metrics,
        state=state,
        calibration_eligible=False,
        persistence_mode="SUPABASE",
        note="open",
    )


def test_config_requires_url_and_key_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRYPTO_SCANNER_SUPABASE_URL", "https://abc.supabase.co")
    monkeypatch.delenv("CRYPTO_SCANNER_SUPABASE_SERVICE_ROLE_KEY", raising=False)
    with pytest.raises(PersistenceError, match="configured together"):
        SupabasePersistenceConfig.from_environment()


def test_config_rejects_non_supabase_host() -> None:
    config = SupabasePersistenceConfig(
        url="https://example.com",
        service_role_key="secret",
    )
    with pytest.raises(PersistenceError, match="supabase.co"):
        config.validate()


def test_stable_position_id_is_state_independent() -> None:
    assert stable_position_id(_record()) == stable_position_id(_record(TrajectoryState.CLOSED))


def test_supabase_store_upserts_position_trajectory_and_closed_trade() -> None:
    calls: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/schema_meta"):
            return httpx.Response(200, json=[{"value": SCHEMA_VERSION}])
        payload = request.read().decode()
        calls.append((request.url.path, payload))
        return httpx.Response(201, json=[])

    config = SupabasePersistenceConfig(
        url="https://abc.supabase.co",
        service_role_key="top-secret-key",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        store = SupabaseTrajectoryStore(config, client=http_client)
        store.save((_record(TrajectoryState.CLOSED),))

    assert [path for path, _ in calls] == [
        "/rest/v1/positions",
        "/rest/v1/position_trajectory",
        "/rest/v1/closed_trades",
    ]
    combined = "".join(str(payload) for _, payload in calls)
    assert '"1.415"' in combined
    assert '"CLOSED"' in combined
    assert "top-secret-key" not in combined


def test_schema_mismatch_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json=[{"value": "wrong-version"}])

    config = SupabasePersistenceConfig(
        url="https://abc.supabase.co",
        service_role_key="secret",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        store = SupabaseTrajectoryStore(config, client=http_client)
        with pytest.raises(PersistenceError, match="schema mismatch"):
            store.save((_record(),))
