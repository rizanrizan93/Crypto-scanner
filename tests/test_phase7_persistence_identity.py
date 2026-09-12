from __future__ import annotations

import json
from decimal import Decimal

import httpx

from crypto_scanner.closed_trades import TradeDirection
from crypto_scanner.persistence import (
    SCHEMA_VERSION,
    SupabasePersistenceConfig,
    SupabaseTrajectoryStore,
)
from crypto_scanner.trajectory import TrajectoryMetrics, TrajectoryQuality
from crypto_scanner.trajectory_store import TrajectoryRecord, TrajectoryState


def test_phase7_parent_position_has_explicit_reconstruction_identity() -> None:
    captured_position: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/schema_meta"):
            return httpx.Response(200, json=[{"value": SCHEMA_VERSION}])
        if request.method == "POST" and request.url.path.endswith("/positions"):
            payload = json.loads(request.read().decode())
            assert isinstance(payload, list) and len(payload) == 1
            captured_position.update(payload[0])
        return httpx.Response(201, json=[])

    snapshot = TrajectoryMetrics(
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
    record = TrajectoryRecord(
        snapshot=snapshot,
        state=TrajectoryState.OPEN,
        calibration_eligible=False,
        persistence_mode="SUPABASE",
        note="open",
    )
    config = SupabasePersistenceConfig(
        url="https://abc.supabase.co",
        service_role_key="secret",
    )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http_client,
        SupabaseTrajectoryStore(config, client=http_client) as store,
    ):
        store.save((record,))

    source = captured_position["source"]
    assert isinstance(source, dict)
    assert source["identity_chain"] == "PHASE7_TRAJECTORY_RECONSTRUCTION"
    assert "signal_id" not in captured_position
