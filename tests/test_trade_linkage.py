from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import httpx

from crypto_scanner.discovery import DiscoveryResult, DiscoveryStatus, TradeDirection
from crypto_scanner.discovery_pipeline import DiscoveryRun
from crypto_scanner.fast_lane import ReadinessDecision, ReadinessStatus
from crypto_scanner.persistence import SCHEMA_VERSION, SupabasePersistenceConfig
from crypto_scanner.signal_geometry import EntryMode, SignalGeometry
from crypto_scanner.trade_linkage import (
    DurableTradeLinkage,
    stable_position_id_from_episode,
    stable_run_id,
)


def _candidate() -> DiscoveryResult:
    frame = SimpleNamespace(
        timeframe="15",
        regime=SimpleNamespace(regime=SimpleNamespace(value="TREND")),
    )
    return DiscoveryResult(
        symbol="BTCUSDT",
        direction=TradeDirection.LONG,
        status=DiscoveryStatus.CANDIDATE,
        base_long_score=Decimal("80"),
        base_short_score=Decimal("20"),
        long_score=Decimal("82"),
        short_score=Decimal("18"),
        evidence_coverage=Decimal("0.95"),
        frames=(frame,),
        reasons=("DISCOVERY_EVIDENCE_ALIGNED",),
    )


def _geometry() -> SignalGeometry:
    return SignalGeometry(
        symbol="BTCUSDT",
        direction=TradeDirection.LONG,
        entry_mode=EntryMode.HL_PULLBACK,
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        take_profit_1=Decimal("103"),
        take_profit_2=Decimal("105"),
        initial_risk=Decimal("2"),
        rr_tp1=Decimal("1.5"),
        rr_tp2=Decimal("2.5"),
        reference_swing=Decimal("98.5"),
        breakout_level=None,
        atr_3m=Decimal("1"),
        chase_atr=Decimal("0.1"),
    )


def _config() -> SupabasePersistenceConfig:
    return SupabasePersistenceConfig(
        url="https://abc.supabase.co",
        service_role_key="secret",
    )


def test_discovery_and_execution_ready_signal_are_durably_linked() -> None:
    writes: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/schema_meta"):
            return httpx.Response(200, json=[{"value": SCHEMA_VERSION}])
        if request.method == "POST":
            writes.append((request.url.path, json.loads(request.read().decode())))
            return httpx.Response(201, json=[])
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    run = DiscoveryRun(
        started_at_ms=1_000_000,
        completed_at_ms=1_001_000,
        results=(_candidate(),),
        failures=(),
    )
    decision = ReadinessDecision(
        symbol="BTCUSDT",
        status=ReadinessStatus.EXECUTION_READY,
        geometry=_geometry(),
        reasons=("ALL_HARD_GUARDS_PASSED",),
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        linkage = DurableTradeLinkage(_config(), client=client)
        run_id = linkage.save_discovery_run(run, execution_armed=True)
        signal_id = linkage.save_execution_ready_signal(
            run_id=run_id,
            candidate=_candidate(),
            readiness=decision,
            candidate_timestamp_ms=run.completed_at_ms,
            geometry_created_at_ms=run.completed_at_ms + 10,
        )

    assert run_id == stable_run_id(run)
    assert signal_id.startswith("sig-")
    paths = [path for path, _ in writes]
    assert paths == [
        "/rest/v1/scanner_runs",
        "/rest/v1/pair_rankings",
        "/rest/v1/signals",
        "/rest/v1/signal_geometry",
    ]
    scanner_run_payload = writes[0][1][0]
    signal_payload = writes[2][1][0]
    geometry_payload = writes[3][1][0]
    assert scanner_run_payload["execution_armed"] is True
    assert signal_payload["signal_id"] == signal_id
    assert signal_payload["setup"] == "HL_PULLBACK"
    assert signal_payload["regime"] == "TREND"
    assert geometry_payload["signal_id"] == signal_id
    assert geometry_payload["stop_loss"] == "98"


def test_resolve_context_requires_complete_signal_geometry_chain() -> None:
    position_id = stable_position_id_from_episode("BTCUSDT", "LONG", 2_000_000)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/positions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "position_id": position_id,
                        "signal_id": "sig-abc",
                        "initial_stop_loss": "98",
                    }
                ],
            )
        if path.endswith("/signals"):
            return httpx.Response(
                200,
                json=[
                    {
                        "signal_id": "sig-abc",
                        "setup": "HL_PULLBACK",
                        "regime": "TREND",
                        "status": "EXECUTION_READY",
                    }
                ],
            )
        if path.endswith("/signal_geometry"):
            return httpx.Response(200, json=[{"signal_id": "sig-abc", "stop_loss": "98"}])
        raise AssertionError(path)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        linkage = DurableTradeLinkage(_config(), client=client)
        context = linkage.resolve_context(
            symbol="BTCUSDT",
            direction=TradeDirection.LONG,
            entry_time_ms=2_000_000,
        )

    assert context.position_id == position_id
    assert context.signal_id == "sig-abc"
    assert context.initial_stop_loss == Decimal("98")
    assert context.setup == "HL_PULLBACK"
    assert context.regime == "TREND"
    assert context.calibration_eligible
