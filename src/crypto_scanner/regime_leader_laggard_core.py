from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime

from crypto_scanner.adaptive_h4_data import FundingPoint
from crypto_scanner.adaptive_regime_signal import RegimePoint
from crypto_scanner.binance.models import Candle

DAY_MS = 86_400_000
H4_MS = 14_400_000


@dataclass(frozen=True, slots=True)
class DayBar:
    start_ms: int
    open: float
    close: float


@dataclass(frozen=True, slots=True)
class WeekTrade:
    entry_ms: int
    exit_ms: int
    regime: int
    symbols: tuple[str, ...]
    base_return: float
    stress_return: float
    severe_return: float
    price_return: float
    funding_return: float


def resample_daily(rows: tuple[Candle, ...]) -> tuple[DayBar, ...]:
    out: list[DayBar] = []
    for idx in range(0, len(rows) - 5):
        first = rows[idx]
        stamp = datetime.fromtimestamp(first.start_time_ms / 1000, tz=UTC)
        if stamp.hour != 0:
            continue
        block = rows[idx : idx + 6]
        if len(block) != 6:
            continue
        if any(block[j].start_time_ms != first.start_time_ms + j * H4_MS for j in range(6)):
            continue
        out.append(DayBar(first.start_time_ms, float(first.open), float(block[-1].close)))
    return tuple(out)


def _funding_return(points: tuple[FundingPoint, ...], entry: int, exit_: int, direction: int) -> float:
    paid = sum(point.funding_rate for point in points if entry < point.funding_time_ms < exit_)
    return -direction * paid


def build_weekly_trades(
    daily: dict[str, tuple[DayBar, ...]],
    funding: dict[str, tuple[FundingPoint, ...]],
    regimes: tuple[RegimePoint, ...],
) -> tuple[WeekTrade, ...]:
    maps = {symbol: {row.start_ms: row for row in rows} for symbol, rows in daily.items()}
    regime_times = [point.effective_ms for point in regimes]
    btc_days = daily["BTCUSDT"]
    out: list[WeekTrade] = []
    for idx in range(28, len(btc_days) - 8):
        signal = btc_days[idx]
        dt = datetime.fromtimestamp(signal.start_ms / 1000, tz=UTC)
        if dt.weekday() != 0:
            continue
        entry_ms = signal.start_ms + DAY_MS
        exit_ms = entry_ms + 7 * DAY_MS
        pos = bisect_right(regime_times, entry_ms) - 1
        if pos < 0 or regimes[pos].state == 0:
            continue
        regime = regimes[pos].state
        ranked: list[tuple[float, str]] = []
        for symbol, rows in daily.items():
            by_time = maps[symbol]
            current = by_time.get(signal.start_ms)
            prior = by_time.get(signal.start_ms - 28 * DAY_MS)
            entry = by_time.get(entry_ms)
            exit_ = by_time.get(exit_ms)
            if current is None or prior is None or entry is None or exit_ is None or prior.close <= 0 or entry.open <= 0:
                continue
            ranked.append((current.close / prior.close - 1.0, symbol))
        if len(ranked) < 3:
            continue
        ranked.sort()
        selected = ranked[-3:] if regime > 0 else ranked[:3]
        symbols = tuple(symbol for _, symbol in selected)
        price_parts: list[float] = []
        funding_parts: list[float] = []
        for symbol in symbols:
            entry = maps[symbol][entry_ms].open
            exit_ = maps[symbol][exit_ms].open
            price_parts.append(regime * (exit_ / entry - 1.0))
            funding_parts.append(_funding_return(funding[symbol], entry_ms, exit_ms, regime))
        price_return = sum(price_parts) / 3.0
        funding_return = sum(funding_parts) / 3.0
        stressed_funding = funding_return * (0.8 if funding_return >= 0 else 1.2)
        out.append(
            WeekTrade(
                entry_ms,
                exit_ms,
                regime,
                symbols,
                price_return + funding_return - 0.0008,
                price_return + stressed_funding - 0.0014,
                price_return + stressed_funding - 0.0025,
                price_return,
                funding_return,
            )
        )
    return tuple(out)
