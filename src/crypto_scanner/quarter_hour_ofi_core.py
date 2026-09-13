from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import mean, pstdev

from crypto_scanner.funding_carry_research import FundingPoint

CORE6 = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")
Z_THRESHOLD = 1.5
MIN_HISTORY = 2420
ROLLING_DAYS = 28
BASE_BPS = 8.0
STRESS_BPS = 14.0
SEVERE_BPS = 25.0


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    hold_hours: int


CANDIDATES = (
    Candidate("QH_OFI_Z1P5_H4", 4),
    Candidate("QH_OFI_Z1P5_H8", 8),
    Candidate("QH_OFI_Z1P5_H12", 12),
)


@dataclass(frozen=True, slots=True)
class Signal:
    time_ms: int
    zscore: float


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


def build_signals(rows: tuple[tuple[int, float], ...]) -> tuple[Signal, ...]:
    history: deque[tuple[int, float]] = deque()
    output: list[Signal] = []
    window_ms = int(timedelta(days=ROLLING_DAYS).total_seconds() * 1000)
    evaluation_ms = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
    for ts, value in rows:
        cutoff = ts - window_ms
        while history and history[0][0] < cutoff:
            history.popleft()
        if ts >= evaluation_ms and len(history) >= MIN_HISTORY:
            values = [item[1] for item in history]
            sigma = pstdev(values)
            if sigma > 1e-12:
                zscore = (value - mean(values)) / sigma
                if abs(zscore) >= Z_THRESHOLD:
                    output.append(Signal(ts, zscore))
        history.append((ts, value))
    return tuple(output)


def funding_between(points: tuple[FundingPoint, ...], entry_ms: int, exit_ms: int) -> float:
    return sum(p.funding_rate for p in points if entry_ms < p.funding_time_ms < exit_ms)


def simulate_symbol(
    symbol: str,
    signals: tuple[Signal, ...],
    opens: dict[int, float],
    funding: tuple[FundingPoint, ...],
    candidate: Candidate,
) -> tuple[Trade, ...]:
    output: list[Trade] = []
    active_until = -1
    hold_ms = candidate.hold_hours * 3_600_000
    for signal in signals:
        if signal.time_ms < active_until:
            continue
        entry_ms = signal.time_ms + 60_000
        exit_ms = entry_ms + hold_ms
        entry = opens.get(entry_ms)
        exit_price = opens.get(exit_ms)
        if entry is None or exit_price is None or entry <= 0:
            continue
        direction = 1 if signal.zscore >= Z_THRESHOLD else -1
        price_return = direction * (exit_price / entry - 1.0)
        raw_funding = -direction * funding_between(funding, entry_ms, exit_ms)
        stressed_funding = raw_funding * (0.8 if raw_funding >= 0 else 1.2)
        output.append(
            Trade(
                symbol=symbol,
                entry_time_ms=entry_ms,
                exit_time_ms=exit_ms,
                direction=direction,
                price_return=price_return,
                funding_return=raw_funding,
                base_return=price_return + raw_funding - BASE_BPS / 10_000.0,
                stress_return=price_return + stressed_funding - STRESS_BPS / 10_000.0,
                severe_return=price_return + stressed_funding - SEVERE_BPS / 10_000.0,
            )
        )
        active_until = exit_ms
    return tuple(output)
