from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime

from crypto_scanner.adaptive_h4_data import FundingPoint
from crypto_scanner.adaptive_regime_signal import RegimePoint
from crypto_scanner.regime_leader_laggard_core import DAY_MS, DayBar


@dataclass(frozen=True, slots=True)
class WeekTrade:
    entry_ms: int
    exit_ms: int
    regime: int
    symbols: tuple[str, ...]
    base_return: float
    stress_return: float
    severe_return: float


def _funding(points: tuple[FundingPoint, ...], entry: int, exit_: int, direction: int) -> float:
    paid = sum(point.funding_rate for point in points if entry < point.funding_time_ms < exit_)
    return -direction * paid


def build_trades(
    daily: dict[str, tuple[DayBar, ...]],
    funding: dict[str, tuple[FundingPoint, ...]],
    regimes: tuple[RegimePoint, ...],
) -> tuple[WeekTrade, ...]:
    maps = {symbol: {row.start_ms: row for row in rows} for symbol, rows in daily.items()}
    times = [point.effective_ms for point in regimes]
    out: list[WeekTrade] = []
    for signal in daily["BTCUSDT"]:
        if datetime.fromtimestamp(signal.start_ms / 1000, tz=UTC).weekday() != 0:
            continue
        entry_ms = signal.start_ms + DAY_MS
        exit_ms = entry_ms + 7 * DAY_MS
        pos = bisect_right(times, entry_ms) - 1
        if pos < 0 or regimes[pos].state == 0:
            continue
        regime = regimes[pos].state
        ranked: list[tuple[float, str]] = []
        for symbol in daily:
            current = maps[symbol].get(signal.start_ms)
            prior = maps[symbol].get(signal.start_ms - 7 * DAY_MS)
            entry = maps[symbol].get(entry_ms)
            exit_ = maps[symbol].get(exit_ms)
            if current is None or prior is None or entry is None or exit_ is None or prior.close <= 0 or entry.open <= 0:
                continue
            ranked.append((current.close / prior.close - 1.0, symbol))
        if len(ranked) < 3:
            continue
        ranked.sort()
        selected = ranked[:3] if regime > 0 else ranked[-3:]
        symbols = tuple(symbol for _, symbol in selected)
        price = sum(regime * (maps[s][exit_ms].open / maps[s][entry_ms].open - 1.0) for s in symbols) / 3.0
        fund = sum(_funding(funding[s], entry_ms, exit_ms, regime) for s in symbols) / 3.0
        stressed = fund * (0.8 if fund >= 0 else 1.2)
        out.append(WeekTrade(entry_ms, exit_ms, regime, symbols, price + fund - 0.0008, price + stressed - 0.0014, price + stressed - 0.0025))
    return tuple(out)
