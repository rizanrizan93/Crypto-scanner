from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

BASELINE_ROUND_TRIP_COST_BPS = Decimal("8")
_BPS_DENOMINATOR = Decimal("10000")


@dataclass(frozen=True, slots=True)
class CostAdjustedReward:
    gross_rr: Decimal
    round_trip_cost_bps: Decimal
    cost_per_unit: Decimal
    cost_r: Decimal
    net_rr: Decimal


def cost_adjusted_reward_r(
    *,
    entry_price: Decimal,
    initial_risk: Decimal,
    gross_rr: Decimal,
    round_trip_cost_bps: Decimal = BASELINE_ROUND_TRIP_COST_BPS,
) -> CostAdjustedReward:
    """Convert an all-in round-trip cost assumption into R and subtract it from reward.

    The baseline is intentionally the same 8 bps assumption used by historical
    validation. It is an all-in conservative friction budget, not a claim about a
    particular Binance VIP fee tier. Actual Demo commission/funding/slippage is
    measured separately by cost attribution.
    """

    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if initial_risk <= 0:
        raise ValueError("initial_risk must be positive")
    if gross_rr <= 0:
        raise ValueError("gross_rr must be positive")
    if round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps cannot be negative")

    cost_per_unit = entry_price * round_trip_cost_bps / _BPS_DENOMINATOR
    cost_r = cost_per_unit / initial_risk
    return CostAdjustedReward(
        gross_rr=gross_rr,
        round_trip_cost_bps=round_trip_cost_bps,
        cost_per_unit=cost_per_unit,
        cost_r=cost_r,
        net_rr=gross_rr - cost_r,
    )
