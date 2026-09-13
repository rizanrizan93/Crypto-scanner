from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from math import sqrt
from random import Random
from statistics import mean, pstdev

from crypto_scanner.binance.models import Candle

SEED = 20260913


@dataclass(frozen=True, slots=True)
class MomentumCandidate:
    name: str
    lookback_days: int
    top_k: int
    rebalance_days: int
    ema_days: int = 50
    btc_risk_filter: bool = False


FROZEN_CANDIDATES = (
    MomentumCandidate("XSMOM_14D_TOP3_DAILY", 14, 3, 1),
    MomentumCandidate("XSMOM_28D_TOP3_DAILY", 28, 3, 1),
    MomentumCandidate("XSMOM_28D_TOP5_DAILY", 28, 5, 1),
    MomentumCandidate("XSMOM_28D_TOP3_WEEKLY", 28, 3, 7),
    MomentumCandidate("XSMOM_60D_TOP3_WEEKLY_BTC", 60, 3, 7, 100, True),
)


@dataclass(frozen=True, slots=True)
class DailyBar:
    day: date
    open: float
    close: float


@dataclass(frozen=True, slots=True)
class DailyPortfolioReturn:
    entry_day: date
    gross_return: float
    turnover: float
    base_return: float
    stress_return: float
    holdings: tuple[str, ...]


def candles_to_complete_utc_days(candles: tuple[Candle, ...]) -> tuple[DailyBar, ...]:
    by_day: dict[date, list[Candle]] = {}
    for candle in candles:
        stamp = datetime.fromtimestamp(candle.start_time_ms / 1000, tz=UTC)
        by_day.setdefault(stamp.date(), []).append(candle)

    rows: list[DailyBar] = []
    expected_hours = (0, 4, 8, 12, 16, 20)
    for day, items in sorted(by_day.items()):
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


def _ema(values: list[float], span: int) -> float | None:
    if len(values) < span:
        return None
    alpha = 2.0 / (span + 1.0)
    value = mean(values[:span])
    for item in values[span:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def _score_for(
    closes: dict[str, dict[date, float]],
    symbol: str,
    signal_day: date,
    candidate: MomentumCandidate,
) -> float | None:
    series = closes.get(symbol, {})
    days = [day for day in sorted(series) if day <= signal_day]
    if len(days) <= candidate.lookback_days or len(days) < candidate.ema_days:
        return None
    current = series[days[-1]]
    prior = series[days[-1 - candidate.lookback_days]]
    ema = _ema([series[day] for day in days], candidate.ema_days)
    if ema is None or prior <= 0 or current <= ema:
        return None
    momentum = current / prior - 1.0
    if momentum <= 0:
        return None
    return momentum


def select_holdings(
    closes: dict[str, dict[date, float]],
    signal_day: date,
    candidate: MomentumCandidate,
) -> tuple[str, ...]:
    if candidate.btc_risk_filter:
        btc_score = _score_for(closes, "BTCUSDT", signal_day, candidate)
        if btc_score is None:
            return ()
    ranked: list[tuple[float, str]] = []
    for symbol in sorted(closes):
        score = _score_for(closes, symbol, signal_day, candidate)
        if score is not None:
            ranked.append((score, symbol))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return tuple(symbol for _, symbol in ranked[: candidate.top_k])


def _weights(holdings: tuple[str, ...]) -> dict[str, float]:
    if not holdings:
        return {}
    weight = 1.0 / len(holdings)
    return {symbol: weight for symbol in holdings}


def _turnover(old: dict[str, float], new: dict[str, float]) -> float:
    return sum(
        abs(new.get(symbol, 0.0) - old.get(symbol, 0.0))
        for symbol in set(old) | set(new)
    )


def simulate_portfolio(
    daily_by_symbol: dict[str, tuple[DailyBar, ...]],
    candidate: MomentumCandidate,
    *,
    base_round_trip_bps: float = 8.0,
    stress_round_trip_bps: float = 14.0,
    base_carry_bps_per_day: float = 1.0,
    stress_carry_bps_per_day: float = 3.0,
) -> tuple[DailyPortfolioReturn, ...]:
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
    rows: list[DailyPortfolioReturn] = []
    last_rebalance_index: int | None = None

    for idx in range(1, len(calendar) - 1):
        signal_day = calendar[idx - 1]
        entry_day = calendar[idx]
        exit_day = calendar[idx + 1]

        rebalance = (
            last_rebalance_index is None
            or idx - last_rebalance_index >= candidate.rebalance_days
        )
        if rebalance:
            holdings = select_holdings(closes, signal_day, candidate)
            new_weights = _weights(holdings)
            last_rebalance_index = idx
        else:
            new_weights = previous_weights
            holdings = tuple(sorted(new_weights))

        tradable_weights = {
            symbol: weight
            for symbol, weight in new_weights.items()
            if entry_day in opens.get(symbol, {})
            and exit_day in opens.get(symbol, {})
        }
        total_weight = sum(tradable_weights.values())
        if total_weight > 0:
            tradable_weights = {
                symbol: weight / total_weight
                for symbol, weight in tradable_weights.items()
            }
        else:
            tradable_weights = {}

        turnover = _turnover(previous_weights, tradable_weights) if rebalance else 0.0
        gross = 0.0
        for symbol, weight in tradable_weights.items():
            entry = opens[symbol][entry_day]
            exit_px = opens[symbol][exit_day]
            if entry > 0:
                gross += weight * (exit_px / entry - 1.0)

        base_cost = turnover * (base_round_trip_bps / 2.0) / 10_000.0
        stress_cost = turnover * (stress_round_trip_bps / 2.0) / 10_000.0
        invested = sum(tradable_weights.values())
        base_carry = invested * base_carry_bps_per_day / 10_000.0
        stress_carry = invested * stress_carry_bps_per_day / 10_000.0
        rows.append(
            DailyPortfolioReturn(
                entry_day=entry_day,
                gross_return=gross,
                turnover=turnover,
                base_return=gross - base_cost - base_carry,
                stress_return=gross - stress_cost - stress_carry,
                holdings=tuple(sorted(tradable_weights)),
            )
        )
        previous_weights = tradable_weights
    return tuple(rows)


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= max(0.0, 1.0 + value)
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def _total_return(returns: list[float]) -> float:
    equity = 1.0
    for value in returns:
        equity *= max(0.0, 1.0 + value)
    return equity - 1.0


def _block_bootstrap_positive_fraction(
    returns: list[float],
    *,
    trials: int = 100,
    block: int = 14,
) -> float:
    if not returns:
        return 0.0
    rng = Random(SEED)
    positive = 0
    n = len(returns)
    for _ in range(trials):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(returns[(start + offset) % n] for offset in range(block))
        if _total_return(sample[:n]) > 0:
            positive += 1
    return positive / trials


def summarize(
    rows: tuple[DailyPortfolioReturn, ...],
    *,
    field: str,
) -> dict[str, float | int]:
    returns = [float(getattr(row, field)) for row in rows]
    if not returns:
        return {
            "days": 0,
            "total_return": 0.0,
            "annualized_sharpe": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "avg_turnover": 0.0,
            "bootstrap_positive_fraction_100": 0.0,
        }
    sigma = pstdev(returns)
    sharpe = 0.0 if sigma == 0 else sqrt(365.0) * mean(returns) / sigma
    return {
        "days": len(rows),
        "total_return": _total_return(returns),
        "annualized_sharpe": sharpe,
        "max_drawdown": _max_drawdown(returns),
        "win_rate": sum(value > 0 for value in returns) / len(returns),
        "avg_turnover": mean(row.turnover for row in rows),
        "bootstrap_positive_fraction_100": _block_bootstrap_positive_fraction(returns),
    }


def split_calendar(
    rows: tuple[DailyPortfolioReturn, ...],
) -> dict[str, tuple[DailyPortfolioReturn, ...]]:
    return {
        "train": tuple(
            row
            for row in rows
            if date(2024, 1, 1) <= row.entry_day <= date(2024, 12, 31)
        ),
        "validation": tuple(
            row
            for row in rows
            if date(2025, 1, 1) <= row.entry_day <= date(2025, 12, 31)
        ),
        "oos": tuple(
            row
            for row in rows
            if date(2026, 1, 1) <= row.entry_day <= date(2026, 8, 31)
        ),
    }


def candidate_payload(candidate: MomentumCandidate) -> dict[str, object]:
    return asdict(candidate)
