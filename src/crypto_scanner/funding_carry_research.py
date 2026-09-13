from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
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
class FundingCarryCandidate:
    name: str
    lookback_days: int
    top_k: int
    rebalance_days: int


FROZEN_CANDIDATES = (
    FundingCarryCandidate("FCR_3D_TOP3_DAILY", 3, 3, 1),
    FundingCarryCandidate("FCR_7D_TOP3_DAILY", 7, 3, 1),
    FundingCarryCandidate("FCR_7D_TOP5_DAILY", 7, 5, 1),
    FundingCarryCandidate("FCR_14D_TOP3_3DAY", 14, 3, 3),
    FundingCarryCandidate("FCR_28D_TOP3_WEEKLY", 28, 3, 7),
)


@dataclass(frozen=True, slots=True)
class DailyBar:
    day: date
    open: float
    close: float


@dataclass(frozen=True, slots=True)
class FundingPoint:
    funding_time_ms: int
    funding_rate: float

    @property
    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.funding_time_ms / 1000, tz=UTC)


@dataclass(frozen=True, slots=True)
class PortfolioDay:
    day: date
    price_return: float
    funding_return: float
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


def _funding_score(
    points: tuple[FundingPoint, ...],
    signal_day: date,
    candidate: FundingCarryCandidate,
) -> float | None:
    start_day = signal_day - timedelta(days=candidate.lookback_days - 1)
    selected = [
        point
        for point in points
        if start_day <= point.timestamp.date() <= signal_day
    ]
    # Most contracts settle roughly three times/day. Requiring two observations
    # per day tolerates interval changes while rejecting sparse/pre-listing data.
    if len(selected) < 2 * candidate.lookback_days:
        return None
    return sum(point.funding_rate for point in selected) / candidate.lookback_days


def select_sides(
    funding_by_symbol: dict[str, tuple[FundingPoint, ...]],
    signal_day: date,
    candidate: FundingCarryCandidate,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    scored: list[tuple[float, str]] = []
    for symbol in FIXED_UNIVERSE:
        score = _funding_score(
            funding_by_symbol.get(symbol, ()),
            signal_day,
            candidate,
        )
        if score is not None:
            scored.append((score, symbol))
    if len(scored) < 2 * candidate.top_k:
        return (), ()
    scored.sort(key=lambda item: (item[0], item[1]))
    # Negative funding: longs receive funding. Positive funding: shorts receive.
    longs = tuple(symbol for _, symbol in scored[: candidate.top_k])
    shorts = tuple(symbol for _, symbol in scored[-candidate.top_k :])
    return longs, shorts


def _weights(
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
    output: dict[str, float] = {}
    for symbol, value in longs.items():
        output[symbol] = 0.5 * value / long_total
    for symbol, value in shorts.items():
        output[symbol] = 0.5 * value / short_total
    return output


def _turnover(old: dict[str, float], new: dict[str, float]) -> float:
    return sum(
        abs(new.get(symbol, 0.0) - old.get(symbol, 0.0))
        for symbol in set(old) | set(new)
    )


def _strict_intraday_funding_sum(
    points: tuple[FundingPoint, ...],
    *,
    entry_day: date,
    exit_day: date,
) -> float:
    entry = datetime(entry_day.year, entry_day.month, entry_day.day, tzinfo=UTC)
    exit_at = datetime(exit_day.year, exit_day.month, exit_day.day, tzinfo=UTC)
    return sum(
        point.funding_rate
        for point in points
        if entry < point.timestamp < exit_at
    )


def _stress_funding(pnl: float) -> float:
    # Haircut historical funding benefits by 20% and magnify funding costs by 20%.
    return pnl * (0.8 if pnl >= 0 else 1.2)


def simulate(
    daily_by_symbol: dict[str, tuple[DailyBar, ...]],
    funding_by_symbol: dict[str, tuple[FundingPoint, ...]],
    candidate: FundingCarryCandidate,
    *,
    base_round_trip_bps: float = 8.0,
    stress_round_trip_bps: float = 14.0,
) -> tuple[PortfolioDay, ...]:
    opens = {
        symbol: {row.day: row.open for row in rows}
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
            longs, shorts = select_sides(
                funding_by_symbol,
                signal_day,
                candidate,
            )
            target_weights = _weights(longs, shorts)
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
        price_return = 0.0
        funding_return = 0.0
        stress_funding_return = 0.0
        for symbol, weight in weights.items():
            entry = opens[symbol][entry_day]
            exit_price = opens[symbol][exit_day]
            if entry <= 0:
                continue
            price_return += weight * (exit_price / entry - 1.0)
            funding_sum = _strict_intraday_funding_sum(
                funding_by_symbol.get(symbol, ()),
                entry_day=entry_day,
                exit_day=exit_day,
            )
            funding_pnl = -weight * funding_sum
            funding_return += funding_pnl
            stress_funding_return += _stress_funding(funding_pnl)

        gross_exposure = sum(abs(value) for value in weights.values())
        base_cost = turnover * (base_round_trip_bps / 2.0) / 10_000.0
        stress_cost = turnover * (stress_round_trip_bps / 2.0) / 10_000.0
        output.append(
            PortfolioDay(
                day=entry_day,
                price_return=price_return,
                funding_return=funding_return,
                base_return=price_return + funding_return - base_cost,
                stress_return=price_return + stress_funding_return - stress_cost,
                turnover=turnover,
                gross_exposure=gross_exposure,
                long_symbols=tuple(
                    sorted(symbol for symbol, value in weights.items() if value > 0)
                ),
                short_symbols=tuple(
                    sorted(symbol for symbol, value in weights.items() if value < 0)
                ),
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
            "avg_funding_return": 0.0,
            "bootstrap_positive_fraction_200": 0.0,
        }
    sigma = pstdev(values)
    sharpe = 0.0 if sigma == 0 else sqrt(365.0) * mean(values) / sigma
    active_values = [
        value
        for value, row in zip(values, rows, strict=False)
        if row.gross_exposure > 0
    ]
    return {
        "days": len(rows),
        "total_return": _total_return(values),
        "annualized_sharpe": sharpe,
        "max_drawdown": _max_drawdown(values),
        "win_rate": (
            sum(value > 0 for value in active_values) / len(active_values)
            if active_values
            else 0.0
        ),
        "avg_turnover": mean(row.turnover for row in rows),
        "avg_gross_exposure": mean(row.gross_exposure for row in rows),
        "avg_funding_return": mean(row.funding_return for row in rows),
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


def candidate_payload(candidate: FundingCarryCandidate) -> dict[str, object]:
    return asdict(candidate)
