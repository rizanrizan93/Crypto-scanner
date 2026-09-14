from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from crypto_scanner import strategy_pool_runtime as pool_runtime
from crypto_scanner.funding_oi_demo import FUNDING_OI_STRATEGY_ID
from crypto_scanner.persistence import SupabasePersistenceConfig
from crypto_scanner.regime_specialist_demo import REGIME_SPECIALIST_STRATEGY_ID
from crypto_scanner.strategy_pool import (
    PARALLEL_FORWARD_DEMO_STAGE,
    REGIME_FORWARD_DEMO_STAGE,
    DemoPoolStatus,
    seed_strategy_pool,
)
from crypto_scanner.volatility_breakout_demo import VOLATILITY_BREAKOUT_STRATEGY_ID


def _config() -> SupabasePersistenceConfig:
    return SupabasePersistenceConfig(
        url="https://example.supabase.co",
        service_role_key="test-only",
    )


def test_pool_seed_contains_three_frozen_demo_strategies() -> None:
    state = seed_strategy_pool(now_ms=123)
    assert state.revision == 1
    assert state.live_trading_locked
    assert {entry.strategy_id for entry in state.entries} == {
        REGIME_SPECIALIST_STRATEGY_ID,
        VOLATILITY_BREAKOUT_STRATEGY_ID,
        FUNDING_OI_STRATEGY_ID,
    }
    stages = {entry.strategy_id: entry.evidence_stage_filter for entry in state.entries}
    assert stages[REGIME_SPECIALIST_STRATEGY_ID] == REGIME_FORWARD_DEMO_STAGE
    assert stages[VOLATILITY_BREAKOUT_STRATEGY_ID] == PARALLEL_FORWARD_DEMO_STAGE
    assert stages[FUNDING_OI_STRATEGY_ID] == PARALLEL_FORWARD_DEMO_STAGE
    assert all(entry.execution_authorized for entry in state.entries)


def test_parallel_evidence_reader_uses_explicit_stage_filter(monkeypatch) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_signal_ids(
        _config: SupabasePersistenceConfig,
        strategy_id: str,
        *,
        promotion_stage: str | None = None,
    ) -> tuple[str, ...]:
        calls.append((strategy_id, promotion_stage))
        return ()

    monkeypatch.setattr(pool_runtime, "_strategy_signal_ids", fake_signal_ids)
    results, incidents, evidence = pool_runtime.forward_demo_evidence_for_stage(
        _config(),
        VOLATILITY_BREAKOUT_STRATEGY_ID,
        promotion_stage=PARALLEL_FORWARD_DEMO_STAGE,
    )

    assert calls == [
        (VOLATILITY_BREAKOUT_STRATEGY_ID, PARALLEL_FORWARD_DEMO_STAGE)
    ]
    assert results == ()
    assert incidents == 0
    assert evidence["promotion_stage_filter"] == PARALLEL_FORWARD_DEMO_STAGE


def test_gate_decisions_change_only_demo_pool_status() -> None:
    active = DemoPoolStatus.FORWARD_DEMO_ACTIVE
    assert pool_runtime._status_from_decision(active, "WAIT") is active
    assert (
        pool_runtime._status_from_decision(active, "PROMOTE")
        is DemoPoolStatus.DEMO_VALIDATED
    )
    assert (
        pool_runtime._status_from_decision(active, "ROLLBACK")
        is DemoPoolStatus.DEMO_ROLLED_BACK
    )
    assert (
        pool_runtime._status_from_decision(active, "QUARANTINE")
        is DemoPoolStatus.DEMO_QUARANTINED
    )
    assert DemoPoolStatus.DEMO_VALIDATED.execution_authorized
    assert not DemoPoolStatus.DEMO_ROLLED_BACK.execution_authorized
    assert not DemoPoolStatus.DEMO_QUARANTINED.execution_authorized


def test_terminal_demo_status_cannot_silently_rearm() -> None:
    assert (
        pool_runtime._status_from_decision(
            DemoPoolStatus.DEMO_ROLLED_BACK,
            "PROMOTE",
        )
        is DemoPoolStatus.DEMO_ROLLED_BACK
    )
    assert (
        pool_runtime._status_from_decision(
            DemoPoolStatus.DEMO_QUARANTINED,
            "WAIT",
        )
        is DemoPoolStatus.DEMO_QUARANTINED
    )


def test_pool_ranking_prefers_validated_then_expectancy() -> None:
    state = seed_strategy_pool(now_ms=123)
    regime, vol, funding = state.entries
    regime = replace(
        regime,
        status=DemoPoolStatus.FORWARD_DEMO_ACTIVE,
        metrics={
            "sample_size": 30,
            "expectancy_r": "0.20",
            "profit_factor": "1.30",
            "max_drawdown_r": "2.0",
        },
    )
    vol = replace(
        vol,
        status=DemoPoolStatus.DEMO_VALIDATED,
        metrics={
            "sample_size": 30,
            "expectancy_r": "0.08",
            "profit_factor": "1.20",
            "max_drawdown_r": "1.5",
        },
    )
    funding = replace(
        funding,
        status=DemoPoolStatus.FORWARD_DEMO_ACTIVE,
        metrics={
            "sample_size": 30,
            "expectancy_r": "0.10",
            "profit_factor": "1.50",
            "max_drawdown_r": "1.0",
        },
    )

    ranked = pool_runtime.rank_pool_entries((regime, vol, funding))
    assert ranked[0].strategy_id == VOLATILITY_BREAKOUT_STRATEGY_ID
    assert ranked[0].rank == 1
    assert ranked[1].strategy_id == REGIME_SPECIALIST_STRATEGY_ID
    assert Decimal(str(ranked[1].metrics["expectancy_r"])) == Decimal("0.20")
