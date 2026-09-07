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
    HotWatchStatus,
    classify_hot_watch,
    select_hot_candidates,
)

NOW = 1_800_000_000_000


def _candidate(symbol: str, score: str) -> DiscoveryResult:
    return DiscoveryResult(
        symbol=symbol,
        direction=TradeDirection.LONG,
        status=DiscoveryStatus.CANDIDATE,
        base_long_score=Decimal(score),
        base_short_score=Decimal("10"),
        long_score=Decimal(score),
        short_score=Decimal("10"),
        evidence_coverage=Decimal("1"),
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
