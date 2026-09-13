from decimal import Decimal

from crypto_scanner.cost_attribution import (
    CostTradeSample,
    build_cost_samples,
    summarize_costs,
)
from crypto_scanner.transaction_costs import BASELINE_ROUND_TRIP_COST_BPS


def test_actual_commission_funding_and_entry_slippage_are_attributed() -> None:
    rows = (
        {
            "trade_key": "pos-1",
            "signal_id": "sig-1",
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "entry_qty": "1",
            "average_entry_price": "100",
            "realized_pnl": "1.00",
            "commission": "0.08",
            "funding_fee": "-0.02",
            "net_pnl": "0.90",
            "exit_time_ms": 10,
        },
    )
    samples = build_cost_samples(rows, {"sig-1": Decimal("99.95")})

    assert len(samples) == 1
    sample = samples[0]
    assert sample.commission_bps == Decimal("8")
    assert sample.funding_bps == Decimal("-2")
    assert sample.gross_return_bps == Decimal("100")
    assert sample.net_return_bps == Decimal("90")
    assert sample.adverse_entry_slippage_bps is not None
    assert sample.adverse_entry_slippage_bps > Decimal("5")
    assert sample.execution_friction_bps > Decimal("13")


def test_short_price_improvement_is_not_counted_as_adverse_slippage() -> None:
    rows = (
        {
            "trade_key": "pos-2",
            "signal_id": "sig-2",
            "symbol": "ETHUSDT",
            "direction": "SHORT",
            "entry_qty": "1",
            "average_entry_price": "100.10",
            "realized_pnl": "1",
            "commission": "0.04",
            "funding_fee": "0",
            "net_pnl": "0.96",
            "exit_time_ms": 20,
        },
    )
    sample = build_cost_samples(rows, {"sig-2": Decimal("100")})[0]

    assert sample.adverse_entry_slippage_bps == Decimal("0")


def _sample(friction: str) -> CostTradeSample:
    value = Decimal(friction)
    return CostTradeSample(
        symbol="BTCUSDT",
        commission=Decimal("0.01"),
        funding_fee=Decimal("0"),
        realized_pnl=Decimal("1"),
        net_pnl=Decimal("0.99"),
        gross_return_bps=Decimal("10"),
        commission_bps=value,
        funding_bps=Decimal("0"),
        net_return_bps=Decimal("9"),
        adverse_entry_slippage_bps=Decimal("0"),
        execution_friction_bps=value,
    )


def test_learned_cost_remains_observe_only_until_30_samples() -> None:
    summary = summarize_costs(tuple(_sample("12") for _ in range(29)))

    assert summary["learned_cost_activation_eligible"] is False
    assert summary["recommended_round_trip_cost_bps"] == BASELINE_ROUND_TRIP_COST_BPS


def test_30_samples_can_recommend_more_conservative_cost_budget() -> None:
    summary = summarize_costs(tuple(_sample("12") for _ in range(30)))

    assert summary["learned_cost_activation_eligible"] is True
    assert summary["recommended_round_trip_cost_bps"] == Decimal("12")
