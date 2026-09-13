from __future__ import annotations

import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from random import Random
from statistics import mean

from crypto_scanner.run_funding_carry_research import fetch_funding_history, fetch_price_history
from crypto_scanner.squeeze_breakout_engine import Result, simulate_symbol
from crypto_scanner.squeeze_breakout_signal import UNIVERSE, build_setups

SEED = 20260913


def bounds(label: str) -> tuple[date, date]:
    return {
        "train": (date(2024, 1, 1), date(2024, 12, 31)),
        "validation": (date(2025, 1, 1), date(2025, 12, 31)),
        "oos": (date(2026, 1, 1), date(2026, 8, 31)),
    }[label]


def bootstrap(values: list[float]) -> float:
    if not values:
        return 0.0
    rng = Random(SEED)
    n = len(values)
    positive = 0
    for _ in range(200):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(values[(start + i) % n] for i in range(14))
        positive += sum(sample[:n]) > 0
    return positive / 200


def summary(results: tuple[Result, ...], field: str, label: str) -> dict[str, object]:
    start, end = bounds(label)
    selected = tuple(
        result for result in results
        if start <= datetime.fromtimestamp(result.entry_time_ms / 1000, tz=UTC).date() <= end
    )
    ordered = sorted(selected, key=lambda row: (row.exit_time_ms, row.symbol))
    values = [float(getattr(row, field)) for row in ordered]
    positive_sum = sum(value for value in values if value > 0)
    negative_sum = -sum(value for value in values if value < 0)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    by_symbol: dict[str, list[float]] = defaultdict(list)
    for row, value in zip(ordered, values, strict=False):
        by_symbol[row.symbol].append(value)
    min_trades = 3 if label == "validation" else 2 if label == "oos" else 1
    eligible_symbols = [symbol for symbol, vals in by_symbol.items() if len(vals) >= min_trades]
    positive_symbols = [
        symbol for symbol in eligible_symbols if mean(by_symbol[symbol]) > 0
    ]
    exits = Counter(row.exit_reason for row in ordered)
    return {
        "closed_trades": len(ordered),
        "long_trades": sum(row.direction > 0 for row in ordered),
        "short_trades": sum(row.direction < 0 for row in ordered),
        "expectancy_r": mean(values) if values else 0.0,
        "total_r": sum(values),
        "profit_factor": positive_sum / negative_sum if negative_sum > 0 else (999.0 if positive_sum > 0 else 0.0),
        "max_drawdown_r": max_dd,
        "bootstrap_positive_fraction_200": bootstrap(values),
        "eligible_symbol_count": len(eligible_symbols),
        "positive_expectancy_symbol_count": len(positive_symbols),
        "exit_reasons": dict(exits),
    }


def passes(record: dict[str, object]) -> bool:
    stress = record["stress"]
    severe = record["severe_25bps"]
    assert isinstance(stress, dict) and isinstance(severe, dict)
    v = stress["validation"]
    o = stress["oos"]
    so = severe["oos"]
    assert isinstance(v, dict) and isinstance(o, dict) and isinstance(so, dict)
    return bool(
        int(v["closed_trades"]) >= 80 and int(o["closed_trades"]) >= 50
        and int(v["long_trades"]) >= 15 and int(v["short_trades"]) >= 15
        and int(o["long_trades"]) >= 10 and int(o["short_trades"]) >= 10
        and float(v["expectancy_r"]) >= 0.05 and float(v["profit_factor"]) >= 1.10
        and float(v["max_drawdown_r"]) <= 20.0
        and float(o["expectancy_r"]) >= 0.03 and float(o["profit_factor"]) >= 1.05
        and float(o["max_drawdown_r"]) <= 15.0
        and float(o["bootstrap_positive_fraction_200"]) >= 0.70
        and float(so["expectancy_r"]) > 0.0
        and int(v["eligible_symbol_count"]) >= 8
        and int(o["eligible_symbol_count"]) >= 6
        and int(v["positive_expectancy_symbol_count"]) >= 5
        and int(o["positive_expectancy_symbol_count"]) >= 4
    )


def load_symbol(symbol: str):
    candles, missing_price = fetch_price_history(symbol)
    funding, missing_funding = fetch_funding_history(symbol)
    return symbol, candles, funding, missing_price, missing_funding


def main() -> int:
    market = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(load_symbol, symbol): symbol for symbol in UNIVERSE}
        for future in as_completed(futures):
            symbol, candles, funding, missing_price, missing_funding = future.result()
            market[symbol] = (candles, funding)
            print(
                "SQZ_COVERAGE", symbol, len(candles), len(funding),
                len(missing_price), len(missing_funding)
            )
    all_results: list[Result] = []
    setup_counts = {}
    for symbol in UNIVERSE:
        candles, funding = market[symbol]
        setups = build_setups(candles)
        setup_counts[symbol] = len(setups)
        all_results.extend(simulate_symbol(symbol, candles, funding, setups))
    results = tuple(sorted(all_results, key=lambda row: (row.entry_time_ms, row.symbol)))
    record = {
        "candidate": "SQZ_ATR20P_BREAK20_EXP1P5_SL1P5_TP3_H18",
        "base": {p: summary(results, "base_r", p) for p in ("train", "validation", "oos")},
        "stress": {p: summary(results, "stress_r", p) for p in ("train", "validation", "oos")},
        "severe_25bps": {p: summary(results, "severe_r", p) for p in ("train", "validation", "oos")},
    }
    record["historical_pass"] = passes(record)
    report = {
        "schema_version": "CRYPTO_SQUEEZE_BREAKOUT_V1",
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "frozen_before_test": True,
        "selection_partition": "validation",
        "oos_used_for_selection": False,
        "direction_contract": "symmetric LONG/SHORT squeeze-to-expansion breakout",
        "setup_counts": setup_counts,
        "historical_pass": [record["candidate"]] if record["historical_pass"] else [],
        "rows": [record],
    }
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
