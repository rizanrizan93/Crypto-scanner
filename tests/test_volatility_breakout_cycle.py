from types import SimpleNamespace

from crypto_scanner.hot_watch import DEMO_ACQUISITION_REASON
from crypto_scanner.volatility_breakout_cycle import (
    VOL_BREAKOUT_MICRO_CONFIRMATION_ROUNDS,
    _should_retry_temporal_microstructure,
)


def _candidate(*, promoted: bool):
    reasons = (DEMO_ACQUISITION_REASON,) if promoted else ("DISCOVERY_EVIDENCE_ALIGNED",)
    return SimpleNamespace(reasons=reasons)


def _decision(*reasons: str):
    return SimpleNamespace(reasons=reasons)


def test_promoted_watch_retries_micro_alignment_until_third_round() -> None:
    candidate = _candidate(promoted=True)
    decision = _decision("TAKER_PRESSURE_NOT_ALIGNED")

    assert _should_retry_temporal_microstructure(candidate, decision, round_index=1)
    assert _should_retry_temporal_microstructure(candidate, decision, round_index=2)
    assert not _should_retry_temporal_microstructure(
        candidate,
        decision,
        round_index=VOL_BREAKOUT_MICRO_CONFIRMATION_ROUNDS,
    )


def test_strict_candidate_does_not_get_temporal_override() -> None:
    assert not _should_retry_temporal_microstructure(
        _candidate(promoted=False),
        _decision("ORDERBOOK_NOT_ALIGNED"),
        round_index=1,
    )


def test_hard_guard_failure_never_retries_temporal_confirmation() -> None:
    assert not _should_retry_temporal_microstructure(
        _candidate(promoted=True),
        _decision("SPREAD_TOO_WIDE"),
        round_index=1,
    )
    assert not _should_retry_temporal_microstructure(
        _candidate(promoted=True),
        _decision("ORDERBOOK_NOT_ALIGNED", "3M_DATA_STALE"),
        round_index=1,
    )
