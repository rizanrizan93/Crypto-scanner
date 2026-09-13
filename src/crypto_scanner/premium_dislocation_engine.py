from __future__ import annotations

from dataclasses import dataclass

from crypto_scanner.funding_carry_research import FundingPoint
from crypto_scanner.premium_dislocation_signal import Signal

BASE_BPS = 8.0
STRESS_BPS = 14.0
SEVERE_BPS = 25.0


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    hold_hours: int


CANDIDATES = (
    Candidate("PDR_Z1P5_F3D_H8", 8),
    Candidate("PDR_Z1P5_F3D_H24", 24),
)


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    entry_time_ms: int
    exit_time_ms: int
    direction: int
    price_return: float
    funding_return: float
    base_return: float
    stress_return: float
    severe_return: float


def simulate_symbol(
    symbol: str,
    signals: tuple[Signal, ...],
    hourly_opens: dict[int, float],
    funding: tuple[FundingPoint, ...],
    candidate: Candidate,
) -> tuple[Trade, ...]:
    output: list[Trade] = []
    active_until = -1
    hold_ms = candidate.hold_hours * 3_600_000
    for signal in signals:
        if signal.time_ms < active_until:
            continue
        entry_ms = signal.time_ms
        exit_ms = entry_ms + hold_ms
        entry = hourly_opens.get(entry_ms)
        exit_price = hourly_opens.get(exit_ms)
        if entry is None or exit_price is None or entry <= 0:
            continue
        price_return = signal.direction * (exit_price / entry - 1.0)
        funding_sum = sum(
            point.funding_rate
            for point in funding
            if entry_ms < point.funding_time_ms < exit_ms
        )
        raw_funding = -signal.direction * funding_sum
        stressed_funding = raw_funding * (0.8 if raw_funding >= 0 else 1.2)
        output.append(
            Trade(
                symbol=symbol,
                entry_time_ms=entry_ms,
                exit_time_ms=exit_ms,
                direction=signal.direction,
                price_return=price_return,
                funding_return=raw_funding,
                base_return=price_return + raw_funding - BASE_BPS / 10_000.0,
                stress_return=price_return + stressed_funding - STRESS_BPS / 10_000.0,
                severe_return=price_return + stressed_funding - SEVERE_BPS / 10_000.0,
            )
        )
        active_until = exit_ms
    return tuple(output)
