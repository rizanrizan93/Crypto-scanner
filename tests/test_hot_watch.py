from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_scanner.discovery import (
    DiscoveryResult,
    DiscoveryStatus,
    MarketContextBias,
    TradeDirection,
)
from crypto_scanner.fast_lane import ReadinessDecision, ReadinessStatus
from crypto_scanner.hot_watch import (
    DEMO_ACQUISITION_REASON,
    HotWatchStatus,
    classify_hot_watch,
    select_hot_candidates,
)

NOW = 1_800_000_000_000


def _candidate(
    symbol: str,
    score: str,
    *,
    status: DiscoveryStatus = DiscoveryStatus.CANDIDATE,
    coverage: str = "1",
    direction: TradeDirection = TradeDirection.LONG,
) -> DiscoveryResult:
    return DiscoveryResult(
        symbol=symbol,
        direction=direction,
        status=status,
        base_long_score=Decimal(score),
        base_short_score=Decimal("10"),
        long_score=Decimal(score),
        short_score=Decimal("10"),
        evidence_coverage=Decimal(coverage),
        frames=(),
        reasons=("candidate",),
        context_bias=MarketContextBias.BULLISH,
    )


def _decision(*reasons: str, ready: bool = False) -> ReadinessDecision:
    return ReadinessDecision(
        symbol="BTCUSDT",
        status=(
            ReadinessStatus.EXECUTION_READY
            if ready
            else ReadinessStatus.REJECTED
        ),
        geometry=None,
        reasons=("ALL_HARD_GUARDS_PASSED",) if ready else reasons,
    )


def test_hot_candidates_are_bounded_and_ranked() -> None:
    results = tuple(
        _candidate(symbol, score)
        for symbol, score in (
            ("BTCUSDT", "80"),
            ("ETHUSDT", "75"),
            ("SOLUSDT", "90"),
            ("XRPUSDT", "70"),
            ("BNBUSDT", "85"),
        )
    )
    selected = select_hot_candidates(results)
    assert tuple(item.symbol for item in selected) == (
        "SOLUSDT",
        "BNBUSDT",
        "BTCUSDT",
        "ETHUSDT",
    )


def test_demo_acquisition_is_disabled_when_execution_is_not_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRYPTO_SCANNER_TESTNET_EXECUTION", "DISABLED")
    results = (
        _candidate("BTCUSDT", "64", status=DiscoveryStatus.WATCH),
        _candidate("ETHUSDT", "63", status=DiscoveryStatus.WATCH),
    )
    assert select_hot_candidates(results) == ()


def test_demo_acquisition_promotes_high_quality_watch_with_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRYPTO_SCANNER_TESTNET_EXECUTION", "ENABLED")
    strict = _candidate("SOLUSDT", "70")
    promoted = _candidate("BTCUSDT", "64", status=DiscoveryStatus.WATCH)
    selected = select_hot_candidates((promoted, strict))

    assert tuple(item.symbol for item in selected) == ("SOLUSDT", "BTCUSDT")
    assert selected[1].status is DiscoveryStatus.CANDIDATE
    assert DEMO_ACQUISITION_REASON in selected[1].reasons
    assert promoted.status is DiscoveryStatus.WATCH
    assert DEMO_ACQUISITION_REASON not in promoted.reasons


@pytest.mark.parametrize(
    ("score", "coverage", "direction"),
    [
        ("59.99", "1", TradeDirection.LONG),
        ("64", "0.71", TradeDirection.LONG),
        ("64", "1", TradeDirection.NEUTRAL),
    ],
)
def test_demo_acquisition_does_not_promote_below_bounded_floor(
    monkeypatch: pytest.MonkeyPatch,
    score: str,
    coverage: str,
    direction: TradeDirection,
) -> None:
    monkeypatch.setenv("CRYPTO_SCANNER_TESTNET_EXECUTION", "ENABLED")
    result = _candidate(
        "BTCUSDT",
        score,
        status=DiscoveryStatus.WATCH,
        coverage=coverage,
        direction=direction,
    )
    assert select_hot_candidates((result,)) == ()


def test_transient_microstructure_rejection_remains_watching() -> None:
    decision = _decision("ORDERBOOK_NOT_ALIGNED", "TAKER_PRESSURE_NOT_ALIGNED")
    assert (
        classify_hot_watch(
            decision,
            candidate_timestamp_ms=NOW - 60_000,
            now_ms=NOW,
        )
        is HotWatchStatus.WATCHING
    )


@pytest.mark.parametrize("reason", ["CHASE_TOO_FAR", "RR_TOO_LOW", "GEOMETRY_INVALID:test"])
def test_geometry_failure_invalidates_watch(reason: str) -> None:
    assert (
        classify_hot_watch(
            _decision(reason),
            candidate_timestamp_ms=NOW - 60_000,
            now_ms=NOW,
        )
        is HotWatchStatus.INVALIDATED
    )


def test_candidate_expires_without_threshold_relaxation() -> None:
    assert (
        classify_hot_watch(
            _decision("ORDERBOOK_NOT_ALIGNED"),
            candidate_timestamp_ms=NOW - 300_001,
            now_ms=NOW,
        )
        is HotWatchStatus.EXPIRED
    )


def test_execution_ready_terminates_watch() -> None:
    assert (
        classify_hot_watch(
            _decision(ready=True),
            candidate_timestamp_ms=NOW - 60_000,
            now_ms=NOW,
        )
        is HotWatchStatus.EXECUTION_READY
    )
