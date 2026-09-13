from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import sqrt
from statistics import mean, pstdev

from crypto_scanner.funding_carry_research import (
    FIXED_UNIVERSE,
    DailyBar,
    FundingCarryCandidate,
    FundingPoint,
    PortfolioDay,
    _block_bootstrap_positive_fraction,
    _max_drawdown,
    _stress_funding,
    _total_return,
    candles_to_complete_utc_days,
    select_sides,
    split_calendar,
)
from crypto_scanner.run_funding_carry_research import (
    fetch_funding_history,
    fetch_price_history,
)

MAX_SYMBOL_WORKERS = 5
BETA_WINDOW_DAYS = 60
MAX_ABS_BTC_HEDGE = 0.50
BASE_ROUND_TRIP_BPS = 8.0
STRESS_ROUND_TRIP_BPS = 14.0
BTC = "BTCUSDT"

FROZEN_V2 = (
    FundingCarryCandidate("FCR_BH_14D_TOP3_3DAY_BETA60", 14, 3, 3),
    FundingCarryCandidate("FCR_BH_28D_TOP3_WEEKLY_BETA60", 28, 3, 7),
)


@dataclass(frozen=True, slots=True)
class BetaSnapshot:
    day: date
    beta_by_symbol: dict[str, float]


def _load_symbol(symbol: str):
    candles, missing_price = fetch_price_history(symbol)
    funding, missing_funding = fetch_funding_history(symbol)
    return symbol, candles, funding, missing_price, missing_funding


def _close_map(rows: tuple[DailyBar, ...]) -> dict[date, float]:
    return {row.day: row.close for row in rows}


def _open_map(rows: tuple[DailyBar, ...]) -> dict[date, float]:
    return {row.day: row.open for row in rows}


def _beta_for_day(
    symbol: str,
    signal_day: date,
    closes: dict[str, dict[date, float]],
) -> float | None:
    if symbol == BTC:
        return 1.0
    common = sorted(
        day
        for day in set(closes[symbol]) & set(closes[BTC])
        if day <= signal_day
    )
    if len(common) < BETA_WINDOW_DAYS + 1:
        return None
    days = common[-(BETA_WINDOW_DAYS + 1):]
    asset = [
        closes[symbol][days[i]] / closes[symbol][days[i - 1]] - 1.0
        for i in range(1, len(days))
    ]
    market = [
        closes[BTC][days[i]] / closes[BTC][days[i - 1]] - 1.0
        for i in range(1, len(days))
    ]
    market_mean = mean(market)
    asset_mean = mean(asset)
    variance = mean((value - market_mean) ** 2 for value in market)
    if variance <= 1e-12:
        return None
    covariance = mean(
        (a - asset_mean) * (m - market_mean)
        for a, m in zip(asset, market, strict=True)
    )
    beta = covariance / variance
    return max(-3.0, min(3.0, beta))


def _base_weights(longs: tuple[str, ...], shorts: tuple[str, ...]) -> dict[str, float]:
    if not longs or not shorts:
        return {}
    weights = {symbol: 0.5 / len(longs) for symbol in longs}
    weights.update({symbol: -0.5 / len(shorts) for symbol in shorts})
    return weights


def _beta_hedged_weights(
    base: dict[str, float],
    signal_day: date,
    closes: dict[str, dict[date, float]],
) -> tuple[dict[str, float], float, float] | None:
    if not base:
        return None
    betas = {}
    for symbol in base:
        beta = _beta_for_day(symbol, signal_day, closes)
        if beta is None:
            return None
        betas[symbol] = beta
    portfolio_beta = sum(base[symbol] * betas[symbol] for symbol in base)
    hedge = max(-MAX_ABS_BTC_HEDGE, min(MAX_ABS_BTC_HEDGE, -portfolio_beta))
    raw = dict(base)
    raw[BTC] = raw.get(BTC, 0.0) + hedge
    gross = sum(abs(value) for value in raw.values())
    if gross <= 0:
        return None
    weights = {symbol: value / gross for symbol, value in raw.items() if abs(value) > 1e-12}
    post_beta = 0.0
    for symbol, weight in weights.items():
        beta = 1.0 if symbol == BTC else _beta_for_day(symbol, signal_day, closes)
        if beta is None:
            return None
        post_beta += weight * beta
    return weights, portfolio_beta, post_beta


def _turnover(old: dict[str, float], new: dict[str, float]) -> float:
    return sum(
        abs(new.get(symbol, 0.0) - old.get(symbol, 0.0))
        for symbol in set(old) | set(new)
    )


def _funding_sum(
    points: tuple[FundingPoint, ...],
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


def simulate_beta_hedged(
    daily: dict[str, tuple[DailyBar, ...]],
    funding: dict[str, tuple[FundingPoint, ...]],
    candidate: FundingCarryCandidate,
) -> tuple[tuple[PortfolioDay, ...], list[dict[str, float | str]]]:
    opens = {symbol: _open_map(rows) for symbol, rows in daily.items()}
    closes = {symbol: _close_map(rows) for symbol, rows in daily.items()}
    calendar = sorted({row.day for rows in daily.values() for row in rows})
    previous_weights: dict[str, float] = {}
    last_rebalance_index: int | None = None
    output: list[PortfolioDay] = []
    beta_log: list[dict[str, float | str]] = []

    for idx in range(1, len(calendar) - 1):
        signal_day = calendar[idx - 1]
        entry_day = calendar[idx]
        exit_day = calendar[idx + 1]
        rebalance = (
            last_rebalance_index is None
            or idx - last_rebalance_index >= candidate.rebalance_days
        )
        if rebalance:
            longs, shorts = select_sides(funding, signal_day, candidate)
            base = _base_weights(longs, shorts)
            hedged = _beta_hedged_weights(base, signal_day, closes)
            if hedged is None:
                target_weights = {}
                pre_beta = post_beta = 0.0
            else:
                target_weights, pre_beta, post_beta = hedged
            last_rebalance_index = idx
            beta_log.append(
                {
                    "signal_day": signal_day.isoformat(),
                    "pre_hedge_beta": pre_beta,
                    "post_hedge_beta": post_beta,
                }
            )
        else:
            target_weights = previous_weights

        if any(
            entry_day not in opens.get(symbol, {}) or exit_day not in opens.get(symbol, {})
            for symbol in target_weights
        ):
            weights = {}
        else:
            weights = target_weights
        turnover = _turnover(previous_weights, weights) if rebalance else 0.0
        price_return = 0.0
        funding_return = 0.0
        stress_funding_return = 0.0
        for symbol, weight in weights.items():
            entry = opens[symbol][entry_day]
            exit_price = opens[symbol][exit_day]
            price_return += weight * (exit_price / entry - 1.0)
            funding_pnl = -weight * _funding_sum(
                funding.get(symbol, ()),
                entry_day,
                exit_day,
            )
            funding_return += funding_pnl
            stress_funding_return += _stress_funding(funding_pnl)

        base_cost = turnover * (BASE_ROUND_TRIP_BPS / 2.0) / 10_000.0
        stress_cost = turnover * (STRESS_ROUND_TRIP_BPS / 2.0) / 10_000.0
        output.append(
            PortfolioDay(
                day=entry_day,
                price_return=price_return,
                funding_return=funding_return,
                base_return=price_return + funding_return - base_cost,
                stress_return=price_return + stress_funding_return - stress_cost,
                turnover=turnover,
                gross_exposure=sum(abs(value) for value in weights.values()),
                long_symbols=tuple(sorted(symbol for symbol, value in weights.items() if value > 0)),
                short_symbols=tuple(sorted(symbol for symbol, value in weights.items() if value < 0)),
            )
        )
        previous_weights = weights
    return tuple(output), beta_log


def _summary(rows: tuple[PortfolioDay, ...], field: str) -> dict[str, float | int]:
    values = [float(getattr(row, field)) for row in rows]
    sigma = pstdev(values) if len(values) > 1 else 0.0
    return {
        "days": len(rows),
        "total_return": _total_return(values),
        "annualized_sharpe": 0.0 if sigma == 0 else sqrt(365.0) * mean(values) / sigma,
        "max_drawdown": _max_drawdown(values),
        "bootstrap_positive_fraction_200": _block_bootstrap_positive_fraction(values),
        "avg_turnover": mean(row.turnover for row in rows) if rows else 0.0,
        "avg_gross_exposure": mean(row.gross_exposure for row in rows) if rows else 0.0,
        "avg_price_return": mean(row.price_return for row in rows) if rows else 0.0,
        "avg_funding_return": mean(row.funding_return for row in rows) if rows else 0.0,
    }


def main() -> int:
    prices = {}
    funding = {}
    missing = {}
    with ThreadPoolExecutor(max_workers=MAX_SYMBOL_WORKERS) as executor:
        futures = {executor.submit(_load_symbol, symbol): symbol for symbol in FIXED_UNIVERSE}
        for future in as_completed(futures):
            symbol, candles, points, missing_price, missing_funding = future.result()
            prices[symbol] = candles
            funding[symbol] = points
            missing[symbol] = {"price": missing_price, "funding": missing_funding}
            print("FCR_BH_COVERAGE", symbol, len(candles), len(points))

    daily = {symbol: candles_to_complete_utc_days(prices[symbol]) for symbol in FIXED_UNIVERSE}
    records = []
    for candidate in FROZEN_V2:
        portfolio, beta_log = simulate_beta_hedged(daily, funding, candidate)
        parts = split_calendar(portfolio)
        beta_abs_pre = [abs(float(row["pre_hedge_beta"])) for row in beta_log]
        beta_abs_post = [abs(float(row["post_hedge_beta"])) for row in beta_log]
        records.append(
            {
                "candidate": candidate.name,
                "parameters": {
                    "lookback_days": candidate.lookback_days,
                    "top_k": candidate.top_k,
                    "rebalance_days": candidate.rebalance_days,
                    "beta_window_days": BETA_WINDOW_DAYS,
                    "max_abs_btc_hedge": MAX_ABS_BTC_HEDGE,
                },
                "base": {label: _summary(rows, "base_return") for label, rows in parts.items()},
                "stress": {label: _summary(rows, "stress_return") for label, rows in parts.items()},
                "beta_diagnostic": {
                    "rebalance_count": len(beta_log),
                    "mean_abs_pre_hedge_beta": mean(beta_abs_pre) if beta_abs_pre else 0.0,
                    "mean_abs_post_hedge_beta": mean(beta_abs_post) if beta_abs_post else 0.0,
                },
            }
        )

    report = {
        "schema_version": "CRYPTO_FUNDING_CARRY_BETA_HEDGE_V2",
        "frozen_before_test": True,
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "promotion_eligible": False,
        "design_note": "post-V1 risk-control hypothesis; all 2023-2026 evidence is diagnostic because V2 was designed after V1 OOS was observed",
        "beta_hedge_contract": {
            "market_proxy": BTC,
            "window_days": BETA_WINDOW_DAYS,
            "estimation": "completed daily close-to-close returns ending on signal_day",
            "max_abs_raw_btc_hedge": MAX_ABS_BTC_HEDGE,
            "gross_normalization": 1.0,
        },
        "costs": {
            "base_round_trip_bps": BASE_ROUND_TRIP_BPS,
            "stress_round_trip_bps": STRESS_ROUND_TRIP_BPS,
            "stress_positive_funding_haircut": 0.20,
            "stress_negative_funding_cost_multiplier": 1.20,
        },
        "rows": records,
        "missing": missing,
    }
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
