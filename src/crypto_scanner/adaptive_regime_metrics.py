from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
from random import Random
from statistics import mean

from crypto_scanner.adaptive_regime_accounting import TradeResult

SEED = 20260913


def _bounds(label: str) -> tuple[date, date]:
    return {
        "train": (date(2024, 1, 1), date(2024, 12, 31)),
        "validation": (date(2025, 1, 1), date(2025, 12, 31)),
        "oos": (date(2026, 1, 1), date(2026, 8, 31)),
    }[label]


def _bootstrap(values: list[float]) -> float:
    if not values:
        return 0.0
    rng = Random(SEED)
    n = len(values)
    positive = 0
    for _ in range(200):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(values[(start + i) % n] for i in range(20))
        positive += sum(sample[:n]) > 0
    return positive / 200


def summarize(trades: tuple[TradeResult, ...], field: str, label: str) -> dict[str, object]:
    start, end = _bounds(label)
    chosen = [t for t in trades if start <= datetime.fromtimestamp(t.entry_ms / 1000, tz=UTC).date() <= end]
    chosen.sort(key=lambda t: (t.exit_ms, t.symbol))
    values = [float(getattr(t, field)) for t in chosen]
    wins = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    by_symbol: dict[str, list[float]] = defaultdict(list)
    for trade, value in zip(chosen, values, strict=False):
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        by_symbol[trade.symbol].append(value)
    min_symbol_trades = 4 if label == "validation" else 3 if label == "oos" else 1
    eligible = [s for s, vals in by_symbol.items() if len(vals) >= min_symbol_trades]
    positive = [s for s in eligible if mean(by_symbol[s]) > 0]
    return {
        "closed_trades": len(chosen),
        "long_trades": sum(t.direction > 0 for t in chosen),
        "short_trades": sum(t.direction < 0 for t in chosen),
        "expectancy_r": mean(values) if values else 0.0,
        "total_r": sum(values),
        "profit_factor": wins / losses if losses > 0 else (999.0 if wins > 0 else 0.0),
        "max_drawdown_r": max_dd,
        "bootstrap_positive_fraction_200": _bootstrap(values),
        "eligible_symbol_count": len(eligible),
        "positive_symbol_count": len(positive),
    }


def historical_pass(stress: dict[str, dict[str, object]], severe: dict[str, dict[str, object]]) -> bool:
    v, o, so = stress["validation"], stress["oos"], severe["oos"]
    return bool(
        int(v["closed_trades"]) >= 120 and int(o["closed_trades"]) >= 70
        and int(v["long_trades"]) >= 25 and int(v["short_trades"]) >= 25
        and int(o["long_trades"]) >= 15 and int(o["short_trades"]) >= 15
        and float(v["expectancy_r"]) >= 0.05 and float(v["profit_factor"]) >= 1.10
        and float(v["max_drawdown_r"]) <= 25.0
        and float(o["expectancy_r"]) >= 0.03 and float(o["profit_factor"]) >= 1.05
        and float(o["max_drawdown_r"]) <= 20.0
        and float(o["bootstrap_positive_fraction_200"]) >= 0.70
        and int(v["eligible_symbol_count"]) >= 10 and int(v["positive_symbol_count"]) >= 6
        and int(o["eligible_symbol_count"]) >= 8 and int(o["positive_symbol_count"]) >= 5
        and float(so["expectancy_r"]) > 0.0
    )
