from __future__ import annotations

from dataclasses import dataclass

from crypto_scanner.adaptive_regime_signal import H4_MS, Setup
from crypto_scanner.binance.models import Candle


@dataclass(frozen=True, slots=True)
class RawTrade:
    symbol: str
    entry_ms: int
    exit_ms: int
    direction: int
    gross_r: float
    risk_return: float
    exit_reason: str


def simulate_price(symbol: str, candles: tuple[Candle, ...], setups: tuple[Setup, ...]) -> tuple[RawTrade, ...]:
    out: list[RawTrade] = []
    active_through = -1
    for setup in setups:
        entry_idx = setup.index + 1
        last_idx = entry_idx + 11
        if entry_idx <= active_through or last_idx >= len(candles):
            continue
        if any(candles[j].start_time_ms - candles[j - 1].start_time_ms != H4_MS for j in range(entry_idx, last_idx + 1)):
            continue
        entry = float(candles[entry_idx].open)
        risk = 1.5 * setup.atr
        if entry <= 0 or risk <= 0:
            continue
        stop = entry - setup.direction * risk
        target = entry + setup.direction * 3.0 * setup.atr
        exit_idx = last_idx
        exit_price = float(candles[last_idx].close)
        reason = "MAX_HOLD"
        for idx in range(entry_idx, last_idx + 1):
            high, low = float(candles[idx].high), float(candles[idx].low)
            stop_hit = low <= stop if setup.direction > 0 else high >= stop
            target_hit = high >= target if setup.direction > 0 else low <= target
            if stop_hit:
                exit_idx, exit_price, reason = idx, stop, "STOP"
                break
            if target_hit:
                exit_idx, exit_price, reason = idx, target, "TARGET"
                break
        risk_return = risk / entry
        gross_r = setup.direction * (exit_price / entry - 1.0) / risk_return
        out.append(RawTrade(symbol, candles[entry_idx].start_time_ms, candles[exit_idx].start_time_ms + H4_MS, setup.direction, gross_r, risk_return, reason))
        active_through = exit_idx
    return tuple(out)
