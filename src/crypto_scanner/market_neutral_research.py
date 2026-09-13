from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from math import sqrt
from random import Random
from statistics import mean, pstdev

from crypto_scanner.binance.models import Candle

SEED = 20260913
FIXED_UNIVERSE = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "DOTUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "TRXUSDT",
    "SUIUSDT",
    "AAVEUSDT",
    "UNIUSDT",
    "ETCUSDT",
    "NEARUSDT",
    "ATOMUSDT",
    "XLMUSDT",
)


@dataclass(frozen=True, slots=True)
class RelativeValueCandidate:
    name: str
    mode: str
    lookback_days: int
    top_k: int
    rebalance_days: int
    vol_normalized: bool = False


FROZEN_CANDIDATES = (
    RelativeValueCandidate("MNV_MOM_28D_TOP3_WEEKLY", "MOM", 28, 3, 7),
    RelativeValueCandidate("MNV_MOM_60D_TOP3_WEEKLY", "MOM", 60, 3, 7),
    RelativeValueCandidate(
        "MNV_MOMVOL_28D_TOP3_WEEKLY", "MOM", 28, 3, 7, True
    ),
    RelativeValueCandidate("MNV_REV_7D_TOP3_DAILY", "REV", 7, 3, 1),
    RelativeValueCandidate("MNV_REV_14D_TOP3_3DAY", "REV", 14, 3, 3),
    RelativeValueCandidate("MNV_REV_28D_TOP3_WEEKLY", "REV", 28, 3, 7),
)


@dataclass(frozen=True, slots=True)
class DailyBar:
    day: date
    open: float
    close: float


@dataclass(frozen=True, slots=True)
class PortfolioDay:
    day: date
    gross_return: float
    base_return: float
    stress_return: float
    turnover: float
    gross_exposure: float
    long_symbols: tuple[str, ...]
    short_symbols: tuple[str, ...]


def candles_to_complete_utc_days(candles: tuple[Candle, ...]) -> tuple[DailyBar, ...]:
    grouped: dict[date, list[Candle]] = {}
    for candle in candles:
        stamp = datetime.fromtimestamp(candle.start_time_ms / 1000, tz=UTC)
        grouped.setdefault(stamp.date(), []).append(candle)

    expected_hours = (0, 4, 8, 12, 16, 20)
    rows: list[DailyBar] = []
    for day, items in sorted(grouped.items()):
        items = sorted(items, key=lambda row: row.start_time_ms)
        hours = tuple(
            datetime.fromtimestamp(row.start_time_ms / 1000, tz=UTC).hour
            for row in items
        )
        if len(items) != 6 or hours != expected_hours:
            continue
        rows.append(
            DailyBar(
                day=day,
                open=float(items[0].open),
                close=float(items[-1].close),
            )
        )
    return tuple(rows)


def _score(
    closes: dict[date, float],
    signal_day: date,
    candidate: RelativeValueCandidate,
) -> float | None:
    days = [day for day in sorted(closes) if day <= signal_day]
    if len(days) <= candidate.lookback_days:
        return None
    current = closes[days[-1]]
    prior = closes[days[-1 - candidate.lookback_days]]
    if current <= 0 or prior <= 0:
        return None
    raw = current / prior - 1.0
    if not candidate.vol_normalized:
        return raw
    returns: list[float] = []
    start = max(1, len(days) - candidate.lookback_days)
    for idx in range(start, len(days)):
        previous = closes[days[idx - 1]]
        value = closes[days[idx]]
        if previous > 0:
            returns.append(value / previous - 1.0)
    if len(returns) < max(5, candidate.lookback_days // 2):
        return None
    sigma = pstdev(returns)
    if sigma <= 0:
        return None
    return raw / sigma


def select_sides(
    closes_by_symbol: dict[str, dict[date, float]],
    signal_day: date,
    candidate: RelativeValueCandidate,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    scored: list[tuple[float, str]] = []
    for symbol in FIXED_UNIVERSE:
        score = _score(closes_by_symbol.get(symbol, {}), signal_day, candidate)
        if score is not None:
            scored.append((score, symbol))
    if len(scored) < 2 * candidate.top_k:
        return (), ()
    scored.sort(key=lambda item: (item[0], item[1]))
    low = tuple(symbol for _, symbol in scored[: candidate.top_k])
    high = tuple(symbol for _, symbol in scored[-candidate.top_k :])
    if candidate.mode == "MOM":
        return high, low
    if candidate.mode == "REV":
        return low, high
    raise ValueError(f"unknown mode: {candidate.mode}")


def _signed_weights(
    longs: tuple[str, ...],
    shorts: tuple[str, ...],
) -> dict[str, float]:
    if not longs or not shorts:
        return {}
    long_weight = 0.5 / len(longs)
    short_weight = -0.5 / len(shorts)
    result = {symbol: long_weight for symbol in longs}
    result.update({symbol: short_weight for symbol in shorts})
    return result


def _renormalize_neutral(weights: dict[str, float]) -> dict[str, float]:
    longs = {symbol: value for symbol, value in weights.items() if value > 0}
    shorts = {symbol: value for symbol, value in weights.items() if value < 0}
    if not longs or not shorts:
        return {}
    long_total = sum(longs.values())
    short_total = -sum(shorts.values())
    result: dict[str, float] = {}
    for symbol, value in longs.items():
        result[symbol] = 0.5 * value / long_total
    for symbol, value in shorts.items():
        result[symbol] = 0.5 * value / short_total
    return result


def _turnover(old: dict[str, float], new: dict[str, float]) -> float:
    return sum(
        abs(new.get(symbol, 0.0) - old.get(symbol, 0.0))
        for symbol in set(old) | set(new)
    )


def simulate(
    daily_by_symbol: dict[str, tuple[DailyBar, ...]],
    candidate: RelativeValueCandidate,
    *,
    base_round_trip_bps: float = 8.0,
    stress_round_trip_bps: float = 14.0,
    base_carry_bps_per_day: float = 1.0,
    stress_carry_bps_per_day: float = 3.0,
) -> tuple[PortfolioDay, ...]:
    opens = {
        symbol: {row.day: row.open for row in rows}
        for symbol, rows in daily_by_symbol.items()
    }
    closes = {
        symbol: {row.day: row.close for row in rows}
        for symbol, rows in daily_by_symbol.items()
    }
    calendar = sorted(
        {row.day for rows in daily_by_symbol.values() for row in rows}
    )
    previous_weights: dict[str, float] = {}
    last_rebalance_index: int | None = None
    output: list[PortfolioDay] = []

    for idx in range(1, len(calendar) - 1):
        signal_day = calendar[idx - 1]
        entry_day = calendar[idx]
        exit_day = calendar[idx + 1]
        rebalance = (
            last_rebalance_index is None
            or idx - last_rebalance_index >= candidate.rebalance_days
        )
        if rebalance:
            longs, shorts = select_sides(closes, signal_day, candidate)
            target_weights = _signed_weights(longs, shorts)
            last_rebalance_index = idx
        else:
            target_weights = previous_weights

        tradable = {
            symbol: weight
            for symbol, weight in target_weights.items()
            if entry_day in opens.get(symbol, {})
            and exit_day in opens.get(symbol, {})
        }
        weights = _renormalize_neutral(tradable)
        turnover = _turnover(previous_weights, weights) if rebalance else 0.0
        gross = 0.0
        for symbol, weight in weights.items():
            entry = opens[symbol][entry_day]
            exit_price = opens[symbol][exit_day]
            if entry > 0:
                gross += weight * (exit_price / entry - 1.0)

        gross_exposure = sum(abs(value) for value in weights.values())
        base_cost = turnover * (base_round_trip_bps / 2.0) / 10_000.0
        stress_cost = turnover * (stress_round_trip_bps / 2.0) / 10_000.0
        base_carry = gross_exposure * base_carry_bps_per_day / 10_000.0
        stress_carry = gross_exposure * stress_carry_bps_per_day / 10_000.0
        long_symbols = tuple(sorted(symbol for symbol, value in weights.items() if value > 0))
        short_symbols = tuple(sorted(symbol for symbol, value in weights.items() if value < 0))
        output.append(
            PortfolioDay(
                day=entry_day,
                gross_return=gross,
                base_return=gross - base_cost - base_carry,
                stress_return=gross - stress_cost - stress_carry,
                turnover=turnover,
                gross_exposure=gross_exposure,
                long_symbols=long_symbols,
                short_symbols=short_symbols,
            )
        )
        previous_weights = weights
    return tuple(output)


def _total_return(values: list[float]) -> float:
    equity = 1.0
    for value in values:
        equity *= max(0.0, 1.0 + value)
    return equity - 1.0


def _max_drawdown(values: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in values:
        equity *= max(0.0, 1.0 + value)
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def _block_bootstrap_positive_fraction(
    values: list[float],
    *,
    trials: int = 200,
    block: int = 14,
) -> float:
    if not values:
        return 0.0
    rng = Random(SEED)
    n = len(values)
    positive = 0
    for _ in range(trials):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(values[(start + offset) % n] for offset in range(block))
        if _total_return(sample[:n]) > 0:
            positive += 1
    return positive / trials


def summarize(
    rows: tuple[PortfolioDay, ...],
    *,
    field: str,
) -> dict[str, float | int]:
    values = [float(getattr(row, field)) for row in rows]
    if not values:
        return {
            "days": 0,
            "total_return": 0.0,
            "annualized_sharpe": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "avg_turnover": 0.0,
            "avg_gross_exposure": 0.0,
            "bootstrap_positive_fraction_200": 0.0,
        }
    sigma = pstdev(values)
    sharpe = 0.0 if sigma == 0 else sqrt(365.0) * mean(values) / sigma
    active_values = [value for value, row in zip(values, rows) if row.gross_exposure > 0]
    win_rate = (
        sum(value > 0 for value in active_values) / len(active_values)
        if active_values
        else 0.0
    )
    return {
        "days": len(rows),
        "total_return": _total_return(values),
        "annualized_sharpe": sharpe,
        "max_drawdown": _max_drawdown(values),
        "win_rate": win_rate,
        "avg_turnover": mean(row.turnover for row in rows),
        "avg_gross_exposure": mean(row.gross_exposure for row in rows),
        "bootstrap_positive_fraction_200": _block_bootstrap_positive_fraction(values),
    }


def split_calendar(
    rows: tuple[PortfolioDay, ...],
) -> dict[str, tuple[PortfolioDay, ...]]:
    return {
        "train": tuple(
            row for row in rows if date(2024, 1, 1) <= row.day <= date(2024, 12, 31)
        ),
        "validation": tuple(
            row for row in rows if date(2025, 1, 1) <= row.day <= date(2025, 12, 31)
        ),
        "oos": tuple(
            row for row in rows if date(2026, 1, 1) <= row.day <= date(2026, 8, 31)
        ),
    }


def candidate_payload(candidate: RelativeValueCandidate) -> dict[str, object]:
    return asdict(candidate)
