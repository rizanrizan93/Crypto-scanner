from __future__ import annotations

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from math import sqrt
from statistics import mean, pstdev

from crypto_scanner.funding_carry_research import (
    FIXED_UNIVERSE,
    FROZEN_CANDIDATES,
    candles_to_complete_utc_days,
    simulate,
    split_calendar,
    summarize,
)
from crypto_scanner.run_funding_carry_research import (
    fetch_funding_history,
    fetch_price_history,
)

MAX_SYMBOL_WORKERS = 5
COST_LADDER_BPS = (14.0, 20.0, 25.0, 30.0, 40.0)


def _load_symbol(symbol: str):
    candles, missing_price = fetch_price_history(symbol)
    funding, missing_funding = fetch_funding_history(symbol)
    return symbol, candles, funding, missing_price, missing_funding


def _total_return(values: list[float]) -> float:
    equity = 1.0
    for value in values:
        equity *= max(0.0, 1.0 + value)
    return equity - 1.0


def _metrics(rows) -> dict[str, float | int]:
    values = [row.stress_return for row in rows]
    sigma = pstdev(values) if len(values) > 1 else 0.0
    return {
        "days": len(rows),
        "total_return": _total_return(values),
        "annualized_sharpe": 0.0 if sigma == 0 else sqrt(365.0) * mean(values) / sigma,
        "avg_turnover": 0.0 if not rows else mean(row.turnover for row in rows),
        "avg_gross_exposure": 0.0 if not rows else mean(row.gross_exposure for row in rows),
    }


def _quarter(day: date) -> str:
    return f"{day.year}-Q{((day.month - 1) // 3) + 1}"


def _quarterly(rows) -> dict[str, dict[str, float | int]]:
    buckets = {}
    for row in rows:
        buckets.setdefault(_quarter(row.day), []).append(row)
    return {key: _metrics(value) for key, value in sorted(buckets.items())}


def _selection_concentration(rows) -> dict[str, object]:
    long_counts: Counter[str] = Counter()
    short_counts: Counter[str] = Counter()
    active_days = 0
    for row in rows:
        if row.gross_exposure <= 0:
            continue
        active_days += 1
        long_counts.update(row.long_symbols)
        short_counts.update(row.short_symbols)
    return {
        "active_days": active_days,
        "top_longs": long_counts.most_common(10),
        "top_shorts": short_counts.most_common(10),
        "distinct_long_symbols": len(long_counts),
        "distinct_short_symbols": len(short_counts),
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
            missing[symbol] = {
                "price": missing_price,
                "funding": missing_funding,
            }
            print(
                "FCR_ROBUST_COVERAGE",
                symbol,
                "price=", len(candles),
                "funding=", len(points),
            )

    daily = {
        symbol: candles_to_complete_utc_days(prices[symbol])
        for symbol in FIXED_UNIVERSE
    }
    rows = []
    for candidate in FROZEN_CANDIDATES:
        cost_results = {}
        canonical_portfolio = None
        for bps in COST_LADDER_BPS:
            portfolio = simulate(
                daily,
                funding,
                candidate,
                base_round_trip_bps=8.0,
                stress_round_trip_bps=bps,
            )
            if bps == 14.0:
                canonical_portfolio = portfolio
            partitions = split_calendar(portfolio)
            cost_results[str(int(bps))] = {
                label: summarize(part, field="stress_return")
                for label, part in partitions.items()
            }
        assert canonical_portfolio is not None
        parts = split_calendar(canonical_portfolio)
        oos_rows = parts["oos"]
        rows.append(
            {
                "candidate": candidate.name,
                "parameters": {
                    "lookback_days": candidate.lookback_days,
                    "top_k": candidate.top_k,
                    "rebalance_days": candidate.rebalance_days,
                },
                "cost_ladder_stress": cost_results,
                "oos_quarterly_stress14": _quarterly(oos_rows),
                "oos_selection_concentration": _selection_concentration(oos_rows),
                "oos_avg_price_return": mean(row.price_return for row in oos_rows),
                "oos_avg_funding_return": mean(row.funding_return for row in oos_rows),
            }
        )

    report = {
        "schema_version": "CRYPTO_FUNDING_CARRY_ROBUSTNESS_V1",
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "promotion_eligible": False,
        "reason": "post-historical-gate diagnostic; no threshold changes and no retuning",
        "universe": list(FIXED_UNIVERSE),
        "cost_ladder_bps": list(COST_LADDER_BPS),
        "rows": rows,
        "missing": missing,
    }
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
