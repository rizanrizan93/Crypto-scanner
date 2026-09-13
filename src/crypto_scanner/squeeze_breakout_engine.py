from __future__ import annotations

from dataclasses import dataclass

from crypto_scanner.binance.models import Candle
from crypto_scanner.funding_carry_research import FundingPoint
from crypto_scanner.squeeze_breakout_signal import BAR_MS, Setup

STOP_ATR = 1.5
TARGET_ATR = 3.0
MAX_HOLD_BARS = 18
BASE_BPS = 8.0
STRESS_BPS = 14.0
SEVERE_BPS = 25.0


@dataclass(frozen=True, slots=True)
class Result:
    symbol: str
    entry_time_ms: int
    exit_time_ms: int
    direction: int
    gross_r: float
    base_r: float
    stress_r: float
    severe_r: float
    exit_reason: str


def _funding_return(
    points: tuple[FundingPoint, ...],
    entry_ms: int,
    exit_ms: int,
    direction: int,
) -> float:
    paid_rate = sum(
        point.funding_rate
        for point in points
        if entry_ms < point.funding_time_ms < exit_ms
    )
    return -direction * paid_rate


def simulate_symbol(
    symbol: str,
    candles: tuple[Candle, ...],
    funding: tuple[FundingPoint, ...],
    setups: tuple[Setup, ...],
) -> tuple[Result, ...]:
    output: list[Result] = []
    active_through = -1
    for setup in setups:
        entry_idx = setup.index + 1
        if entry_idx <= active_through or entry_idx >= len(candles):
            continue
        last_idx = entry_idx + MAX_HOLD_BARS - 1
        if last_idx >= len(candles):
            continue
        if any(
            candles[j].start_time_ms - candles[j - 1].start_time_ms != BAR_MS
            for j in range(entry_idx, last_idx + 1)
        ):
            continue
        entry = float(candles[entry_idx].open)
        if entry <= 0 or setup.atr <= 0:
            continue
        risk_distance = STOP_ATR * setup.atr
        risk_return = risk_distance / entry
        if risk_return <= 0:
            continue
        if setup.direction > 0:
            stop = entry - risk_distance
            target = entry + TARGET_ATR * setup.atr
        else:
            stop = entry + risk_distance
            target = entry - TARGET_ATR * setup.atr
        exit_price = float(candles[last_idx].close)
        exit_idx = last_idx
        reason = "MAX_HOLD"
        for idx in range(entry_idx, last_idx + 1):
            high = float(candles[idx].high)
            low = float(candles[idx].low)
            if setup.direction > 0:
                stop_hit = low <= stop
                target_hit = high >= target
            else:
                stop_hit = high >= stop
                target_hit = low <= target
            if stop_hit:
                exit_price = stop
                exit_idx = idx
                reason = "STOP"
                break
            if target_hit:
                exit_price = target
                exit_idx = idx
                reason = "TARGET"
                break
        gross_return = setup.direction * (exit_price / entry - 1.0)
        gross_r = gross_return / risk_return
        entry_ms = candles[entry_idx].start_time_ms
        exit_ms = candles[exit_idx].start_time_ms + BAR_MS
        funding_return = _funding_return(funding, entry_ms, exit_ms, setup.direction)
        stress_funding = funding_return * (0.8 if funding_return >= 0 else 1.2)
        base_r = gross_r + (funding_return - BASE_BPS / 10_000.0) / risk_return
        stress_r = gross_r + (stress_funding - STRESS_BPS / 10_000.0) / risk_return
        severe_r = gross_r + (stress_funding - SEVERE_BPS / 10_000.0) / risk_return
        output.append(
            Result(
                symbol=symbol,
                entry_time_ms=entry_ms,
                exit_time_ms=exit_ms,
                direction=setup.direction,
                gross_r=gross_r,
                base_r=base_r,
                stress_r=stress_r,
                severe_r=severe_r,
                exit_reason=reason,
            )
        )
        active_through = exit_idx
    return tuple(output)
