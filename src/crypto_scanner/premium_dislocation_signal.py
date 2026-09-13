from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from statistics import mean, pstdev

from crypto_scanner.funding_carry_research import FundingPoint

CORE6 = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")
Z_THRESHOLD = 1.5
MIN_HISTORY = 605
WINDOW_DAYS = 28
FUNDING_DAYS = 3


@dataclass(frozen=True, slots=True)
class Signal:
    time_ms: int
    direction: int


def trailing_funding(points: tuple[FundingPoint, ...], signal_ms: int) -> float | None:
    window = int(timedelta(days=FUNDING_DAYS).total_seconds() * 1000)
    values = [p.funding_rate for p in points if signal_ms - window <= p.funding_time_ms < signal_ms]
    return mean(values) if len(values) >= 6 else None


def build_signals(
    premium_rows: tuple[tuple[int, float], ...],
    funding: tuple[FundingPoint, ...],
) -> tuple[Signal, ...]:
    history: deque[tuple[int, float]] = deque()
    output: list[Signal] = []
    window = int(timedelta(days=WINDOW_DAYS).total_seconds() * 1000)
    evaluation_start = 1704067200000
    for ts, premium in premium_rows:
        cutoff = ts - window
        while history and history[0][0] < cutoff:
            history.popleft()
        if ts >= evaluation_start and len(history) >= MIN_HISTORY:
            values = [row[1] for row in history]
            sigma = pstdev(values)
            funding_score = trailing_funding(funding, ts)
            if sigma > 1e-12 and funding_score is not None:
                zscore = (premium - mean(values)) / sigma
                if zscore <= -Z_THRESHOLD and funding_score < 0:
                    output.append(Signal(ts, 1))
                elif zscore >= Z_THRESHOLD and funding_score > 0:
                    output.append(Signal(ts, -1))
        history.append((ts, premium))
    return tuple(output)
