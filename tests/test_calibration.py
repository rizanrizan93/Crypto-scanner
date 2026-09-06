from decimal import Decimal

import pytest

from crypto_scanner.calibration import CalibrationMetrics, propose_parameters
from crypto_scanner.strategy_params import StrategyParameters


def test_calibration_observes_only_before_ten_eligible_trades() -> None:
    current = StrategyParameters()
    proposal = propose_parameters(
        CalibrationMetrics(
            sample_size=9,
            win_rate=Decimal("0.44"),
            profit_factor=Decimal("0.80"),
            median_mae_r=Decimal("0.95"),
            median_mfe_r=Decimal("2.20"),
        ),
        current,
    )

    assert proposal.tier == "OBSERVE_ONLY"
    assert proposal.applied is False
    assert proposal.after == current
    assert proposal.reasons == ("INSUFFICIENT_ELIGIBLE_TRADES",)


def test_micro_calibration_can_tighten_entry_widen_stop_and_cap_distant_tp2() -> None:
    current = StrategyParameters()
    proposal = propose_parameters(
        CalibrationMetrics(
            sample_size=10,
            win_rate=Decimal("0.40"),
            profit_factor=Decimal("0.75"),
            median_mae_r=Decimal("0.95"),
            median_mfe_r=Decimal("2.20"),
        ),
        current,
        previous_reviewed_sample_size=0,
    )

    assert proposal.applied is True
    assert proposal.after.max_chase_atr == Decimal("0.78")
    assert proposal.after.stop_buffer_atr == Decimal("0.16")
    assert Decimal("2.00") <= proposal.after.tp2_cap_rr <= Decimal("2.40")
    assert proposal.after.min_rr_tp1 >= Decimal("1.20")
    assert proposal.after.min_rr_tp2 >= Decimal("2.00")


def test_calibration_does_not_reapply_without_enough_new_evidence() -> None:
    current = StrategyParameters(max_chase_atr=Decimal("0.76"))
    proposal = propose_parameters(
        CalibrationMetrics(
            sample_size=12,
            win_rate=Decimal("0.30"),
            profit_factor=Decimal("0.50"),
            median_mae_r=Decimal("0.70"),
            median_mfe_r=Decimal("1.50"),
        ),
        current,
        previous_reviewed_sample_size=10,
    )

    assert proposal.applied is False
    assert proposal.after == current
    assert proposal.reasons == ("WAITING_FOR_NEW_EVIDENCE",)


def test_strategy_parameter_bounds_never_allow_quality_floor_to_loosen() -> None:
    with pytest.raises(ValueError):
        StrategyParameters(max_chase_atr=Decimal("0.81")).validate()
    with pytest.raises(ValueError):
        StrategyParameters(min_rr_tp2=Decimal("1.99")).validate()
    with pytest.raises(ValueError):
        StrategyParameters(stop_buffer_atr=Decimal("0.21")).validate()
    with pytest.raises(ValueError):
        StrategyParameters(tp2_cap_rr=Decimal("1.99")).validate()
