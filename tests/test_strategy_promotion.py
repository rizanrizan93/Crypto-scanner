from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig
from crypto_scanner.strategy_params import StrategyParameters
from crypto_scanner.strategy_promotion import (
    MIN_FORWARD_DEMO_DURATION_MS,
    PromotionStage,
    StrategyPromotionState,
    evaluate_forward_demo_gate,
    evaluate_historical_gate,
    make_strategy_version,
    record_forward_decision,
    record_historical_decision,
    save_promotion_state,
    strategy_version_id,
)
from crypto_scanner.strategy_promotion_runtime import complete_months, robustness_neighbors


def _profitable(count: int) -> tuple[Decimal, ...]:
    return tuple(Decimal(2) if index % 2 == 0 else Decimal(-1) for index in range(count))


def _pending(*, champion: bool = False) -> StrategyPromotionState:
    params = StrategyParameters()
    return StrategyPromotionState(
        PromotionStage.HISTORICAL_PENDING,
        1,
        make_strategy_version(params, source="CHAMPION", created_at_ms=1) if champion else None,
        make_strategy_version(
            StrategyParameters(max_chase_atr=Decimal("0.78")),
            source="CALIBRATION",
            created_at_ms=2,
        ),
        None,
        None,
        2,
        "QUEUED",
    )


def test_strategy_id_is_deterministic() -> None:
    params = StrategyParameters(max_chase_atr=Decimal("0.78"))
    assert strategy_version_id(params) == strategy_version_id(params)
    assert strategy_version_id(params) != strategy_version_id(StrategyParameters())


def test_historical_gate_requires_sample_and_robustness() -> None:
    too_small = evaluate_historical_gate(
        _profitable(199), robustness_result_sets=(_profitable(199), _profitable(199))
    )
    assert not too_small.passed
    passed = evaluate_historical_gate(
        _profitable(240), robustness_result_sets=(_profitable(240), _profitable(240))
    )
    assert passed.passed
    assert passed.oos.sample_size == 120
    assert passed.positive_walk_forward_folds == 4


def test_historical_gate_uses_only_forward_half_for_oos() -> None:
    strong_training = _profitable(100)
    losing_forward = tuple(Decimal("-1") for _ in range(100))
    result = evaluate_historical_gate(
        strong_training + losing_forward,
        robustness_result_sets=(
            strong_training + losing_forward,
            strong_training + losing_forward,
        ),
    )
    assert result.oos.sample_size == 100
    assert result.oos.wins == 0
    assert not result.passed
    assert result.positive_walk_forward_folds == 0


def test_historical_pass_moves_only_to_forward_demo() -> None:
    state = _pending()
    gate = evaluate_historical_gate(
        _profitable(240), robustness_result_sets=(_profitable(240), _profitable(240))
    )
    result = record_historical_decision(state, gate, now_ms=3)
    assert result.stage is PromotionStage.FORWARD_DEMO
    assert result.champion is None


def test_forward_demo_promotes_after_sample_and_duration() -> None:
    gate = evaluate_forward_demo_gate(
        _profitable(30),
        safety_incident_count=0,
        evidence_duration_ms=MIN_FORWARD_DEMO_DURATION_MS,
    )
    forward = record_historical_decision(
        _pending(),
        evaluate_historical_gate(
            _profitable(240), robustness_result_sets=(_profitable(240), _profitable(240))
        ),
        now_ms=3,
    )
    promoted = record_forward_decision(forward, gate, now_ms=4)
    assert promoted.stage is PromotionStage.PROMOTED
    assert promoted.champion == forward.candidate


def test_forward_duration_and_safety_fail_closed() -> None:
    waiting = evaluate_forward_demo_gate(_profitable(30), safety_incident_count=0)
    assert waiting.decision == "WAIT"
    unsafe = evaluate_forward_demo_gate(_profitable(30), safety_incident_count=1)
    assert unsafe.decision == "QUARANTINE"


def test_forward_demo_weak_early_edge_rolls_back() -> None:
    losing = tuple(
        Decimal("0.50") if index % 2 == 0 else Decimal("-1") for index in range(12)
    )
    result = evaluate_forward_demo_gate(losing, safety_incident_count=0)
    assert result.decision == "ROLLBACK"
    assert "FORWARD_DEMO_EARLY_PROFIT_FACTOR_BELOW_0_75" in result.reasons


def test_complete_months_and_neighbors_are_bounded() -> None:
    assert complete_months(3, now=datetime(2026, 1, 10, tzinfo=UTC)) == (
        (2025, 10),
        (2025, 11),
        (2025, 12),
    )
    neighbors = robustness_neighbors(StrategyParameters())
    assert len(neighbors) == 2
    assert len(set(neighbors)) == 2
    for neighbor in neighbors:
        neighbor.validate()


def test_malformed_state_fails_closed() -> None:
    with pytest.raises(PersistenceError, match="schema version"):
        StrategyPromotionState.from_mapping({"schema_version": "wrong"})


def test_promotion_state_transition_uses_revision_compare_and_swap() -> None:
    state = _pending()
    next_state = record_historical_decision(
        state,
        evaluate_historical_gate(
            _profitable(240),
            robustness_result_sets=(_profitable(240), _profitable(240)),
        ),
        now_ms=3,
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"state_key": "strategy_promotion_v1"}])

    config = SupabasePersistenceConfig(
        url="https://abc.supabase.co",
        service_role_key="secret",
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        save_promotion_state(
            config,
            next_state,
            expected_revision=state.revision,
            client=client,
        )

    assert len(requests) == 1
    assert requests[0].method == "PATCH"
    assert requests[0].url.params["version"] == "eq.1"


def test_promotion_state_transition_fails_on_concurrent_revision() -> None:
    state = _pending()
    next_state = record_historical_decision(
        state,
        evaluate_historical_gate(
            _profitable(240),
            robustness_result_sets=(_profitable(240), _profitable(240)),
        ),
        now_ms=3,
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    config = SupabasePersistenceConfig(
        url="https://abc.supabase.co",
        service_role_key="secret",
    )
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(PersistenceError, match="concurrency race"),
    ):
        save_promotion_state(
            config,
            next_state,
            expected_revision=state.revision,
            client=client,
        )
