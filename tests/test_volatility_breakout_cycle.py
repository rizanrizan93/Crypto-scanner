from types import SimpleNamespace

from crypto_scanner.fast_lane import (
    DEMO_TEMPORAL_CONFIRMATION_ROUNDS,
    should_retry_demo_temporal_confirmation,
)
from crypto_scanner.hot_watch import DEMO_ACQUISITION_REASON


def _candidate(*, promoted: bool):
    reasons = (DEMO_ACQUISITION_REASON,) if promoted else ("DISCOVERY_EVIDENCE_ALIGNED",)
    return SimpleNamespace(reasons=reasons)


def _decision(*reasons: str):
    return SimpleNamespace(reasons=reasons)


def test_promoted_watch_retries_micro_alignment_until_third_round(monkeypatch) -> None:
    monkeypatch.setenv("CRYPTO_SCANNER_TESTNET_EXECUTION", "ENABLED")
    candidate = _candidate(promoted=True)
    decision = _decision("TAKER_PRESSURE_NOT_ALIGNED")

    assert should_retry_demo_temporal_confirmation(candidate, decision, round_index=1)
    assert should_retry_demo_temporal_confirmation(candidate, decision, round_index=2)
    assert not should_retry_demo_temporal_confirmation(
        candidate,
        decision,
        round_index=DEMO_TEMPORAL_CONFIRMATION_ROUNDS,
    )


def test_strict_candidate_does_not_get_temporal_override(monkeypatch) -> None:
    monkeypatch.setenv("CRYPTO_SCANNER_TESTNET_EXECUTION", "ENABLED")
    assert not should_retry_demo_temporal_confirmation(
        _candidate(promoted=False),
        _decision("ORDERBOOK_NOT_ALIGNED"),
        round_index=1,
    )


def test_hard_guard_failure_never_retries_temporal_confirmation(monkeypatch) -> None:
    monkeypatch.setenv("CRYPTO_SCANNER_TESTNET_EXECUTION", "ENABLED")
    assert not should_retry_demo_temporal_confirmation(
        _candidate(promoted=True),
        _decision("SPREAD_TOO_WIDE"),
        round_index=1,
    )
    assert not should_retry_demo_temporal_confirmation(
        _candidate(promoted=True),
        _decision("ORDERBOOK_NOT_ALIGNED", "3M_DATA_STALE"),
        round_index=1,
    )
