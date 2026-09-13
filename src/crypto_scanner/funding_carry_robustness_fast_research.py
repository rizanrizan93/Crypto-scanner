from __future__ import annotations

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from math import sqrt
from statistics import mean, pstdev

from crypto_scanner.funding_carry_research import FIXED_UNIVERSE, FROZEN_CANDIDATES, PortfolioDay, _block_bootstrap_positive_fraction, _max_drawdown, _total_return, candles_to_complete_utc_days, simulate, split_calendar
from crypto_scanner.run_funding_carry_research import fetch_funding_history, fetch_price_history

MAX_SYMBOL_WORKERS = 5
CANONICAL_STRESS_BPS = 14.0
COST_LADDER_BPS = (14.0, 20.0, 25.0, 30.0, 40.0)


def _load_symbol(symbol: str):
    candles, missing_price = fetch_price_history(symbol)
    funding, missing_funding = fetch_funding_history(symbol)
    return symbol, candles, funding, missing_price, missing_funding


def _repriced_stress(row: PortfolioDay, bps: float) -> float:
    extra_cost = row.turnover * ((bps - CANONICAL_STRESS_BPS) / 2.0) / 10_000.0
    return row.stress_return - extra_cost


def _summary(rows: tuple[PortfolioDay, ...], bps: float) -> dict[str, float | int]:
    values = [_repriced_stress(row, bps) for row in rows]
    if not values:
        return {"days": 0, "total_return": 0.0, "annualized_sharpe": 0.0, "max_drawdown": 0.0, "bootstrap_positive_fraction_200": 0.0, "avg_turnover": 0.0, "avg_gross_exposure": 0.0}
    sigma = pstdev(values)
    return {"days": len(rows), "total_return": _total_return(values), "annualized_sharpe": 0.0 if sigma == 0 else sqrt(365.0) * mean(values) / sigma, "max_drawdown": _max_drawdown(values), "bootstrap_positive_fraction_200": _block_bootstrap_positive_fraction(values), "avg_turnover": mean(row.turnover for row in rows), "avg_gross_exposure": mean(row.gross_exposure for row in rows)}


def _quarter(day: date) -> str:
    return f"{day.year}-Q{((day.month - 1) // 3) + 1}"


def _quarterly(rows: tuple[PortfolioDay, ...]) -> dict[str, dict[str, float | int]]:
    buckets: dict[str, list[PortfolioDay]] = {}
    for row in rows:
        buckets.setdefault(_quarter(row.day), []).append(row)
    return {key: _summary(tuple(bucket), CANONICAL_STRESS_BPS) for key, bucket in sorted(buckets.items())}


def _selection_concentration(rows: tuple[PortfolioDay, ...]) -> dict[str, object]:
    long_counts: Counter[str] = Counter()
    short_counts: Counter[str] = Counter()
    active_days = 0
    for row in rows:
        if row.gross_exposure <= 0:
            continue
        active_days += 1
        long_counts.update(row.long_symbols)
        short_counts.update(row.short_symbols)
    top_long_share = long_counts.most_common(1)[0][1] / active_days if active_days and long_counts else 0.0
    top_short_share = short_counts.most_common(1)[0][1] / active_days if active_days and short_counts else 0.0
    return {"active_days": active_days, "top_longs": long_counts.most_common(10), "top_shorts": short_counts.most_common(10), "distinct_long_symbols": len(long_counts), "distinct_short_symbols": len(short_counts), "top_long_active_day_share": top_long_share, "top_short_active_day_share": top_short_share}


def _attribution(rows: tuple[PortfolioDay, ...]) -> dict[str, float]:
    if not rows:
        return {"avg_price_return": 0.0, "avg_base_funding_return": 0.0, "avg_stress_funding_return": 0.0, "funding_only_net_total_return_14bps": 0.0}
    stress_funding = [row.stress_return - row.price_return + row.turnover * (CANONICAL_STRESS_BPS / 2.0) / 10_000.0 for row in rows]
    funding_only_net = [value - row.turnover * (CANONICAL_STRESS_BPS / 2.0) / 10_000.0 for row, value in zip(rows, stress_funding, strict=True)]
    return {"avg_price_return": mean(row.price_return for row in rows), "avg_base_funding_return": mean(row.funding_return for row in rows), "avg_stress_funding_return": mean(stress_funding), "funding_only_net_total_return_14bps": _total_return(funding_only_net)}


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
            print("FCR_FAST_COVERAGE", symbol, len(candles), len(points))
    daily = {symbol: candles_to_complete_utc_days(prices[symbol]) for symbol in FIXED_UNIVERSE}
    records = []
    for candidate in FROZEN_CANDIDATES:
        portfolio = simulate(daily, funding, candidate, base_round_trip_bps=8.0, stress_round_trip_bps=CANONICAL_STRESS_BPS)
        parts = split_calendar(portfolio)
        cost_ladder = {str(int(bps)): {label: _summary(rows, bps) for label, rows in parts.items()} for bps in COST_LADDER_BPS}
        records.append({"candidate": candidate.name, "parameters": {"lookback_days": candidate.lookback_days, "top_k": candidate.top_k, "rebalance_days": candidate.rebalance_days}, "cost_ladder_stress": cost_ladder, "oos_quarterly_stress14": _quarterly(parts["oos"]), "oos_selection_concentration": _selection_concentration(parts["oos"]), "attribution": {label: _attribution(rows) for label, rows in parts.items()}})
    report = {"schema_version": "CRYPTO_FUNDING_CARRY_ROBUSTNESS_FAST_V1", "research_only": True, "execution_influence": False, "live_execution_enabled": False, "promotion_eligible": False, "economic_equivalence_note": "positions and funding realization simulated once at 14bps; higher cost ladder values exactly subtract additional turnover cost only", "cost_ladder_bps": list(COST_LADDER_BPS), "rows": records, "missing": missing}
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
