from decimal import Decimal

import pytest

from crypto_scanner.discovery import TradeDirection
from crypto_scanner.fast_lane import ReadinessDecision, ReadinessStatus
from crypto_scanner.managed_scanner_cycle import apply_round_trip_cost_gate
from crypto_scanner.signal_geometry import EntryMode, SignalGeometry
from crypto_scanner.strategy_params import StrategyParameters
from crypto_scanner.transaction_costs import (
    BASELINE_ROUND_TRIP_COST_BPS,
    cost_adjusted_reward_r,
)


def _geometry(rr_tp2: str) -> SignalGeometry:
    return SignalGeometry(
        symbol="BTCUSDT",
        direction=TradeDirection.LONG,
        entry_mode=EntryMode.TECHNICAL_SCALP,
        entry_price=Decimal("100"),
        stop_loss=Decimal("99"),
        take_profit_1=Decimal("101.5"),
        take_profit_2=Decimal("102.2"),
        initial_risk=Decimal("1"),
        rr_tp1=Decimal("1.5"),
        rr_tp2=Decimal(rr_tp2),
        reference_swing=Decimal("99"),
        breakout_level=None,
        atr_3m=Decimal("1"),
        chase_atr=Decimal("0.1"),
    )


def _decision(rr_tp2: str) -> ReadinessDecision:
    return ReadinessDecision(
        symbol="BTCUSDT",
        status=ReadinessStatus.EXECUTION_READY,
        geometry=_geometry(rr_tp2),
        reasons=("ALL_HARD_GUARDS_PASSED",),
    )


def test_baseline_cost_converts_to_r_without_fee_tier_assumption() -> None:
    reward = cost_adjusted_reward_r(
        entry_price=Decimal("100"),
        initial_risk=Decimal("1"),
        gross_rr=Decimal("2.20"),
    )

    assert reward.round_trip_cost_bps == BASELINE_ROUND_TRIP_COST_BPS
    assert reward.cost_per_unit == Decimal("0.08")
    assert reward.cost_r == Decimal("0.08")
    assert reward.net_rr == Decimal("2.12")


def test_managed_gate_rejects_gross_rr_that_falls_below_floor_after_cost() -> None:
    decision = apply_round_trip_cost_gate(_decision("2.05"), StrategyParameters())

    assert decision.status is ReadinessStatus.REJECTED
    assert decision.geometry is None
    assert "NET_RR_AFTER_COST_TOO_LOW" in decision.reasons


def test_managed_gate_keeps_setup_with_sufficient_net_rr() -> None:
    original = _decision("2.20")
    decision = apply_round_trip_cost_gate(original, StrategyParameters())

    assert decision == original


def test_cost_model_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="initial_risk"):
        cost_adjusted_reward_r(
            entry_price=Decimal("100"),
            initial_risk=Decimal("0"),
            gross_rr=Decimal("2"),
        )
