from __future__ import annotations

from datetime import UTC, date, datetime
from math import sqrt
from random import Random
from statistics import mean, pstdev

from crypto_scanner.regime_leader_laggard_core import WeekTrade

SEED = 20260913


def bounds(label: str) -> tuple[date, date]:
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
            sample.extend(values[(start + i) % n] for i in range(4))
        equity = 1.0
        for value in sample[:n]:
            equity *= 1.0 + value
        positive += equity > 1.0
    return positive / 200


def summarize(trades: tuple[WeekTrade, ...], field: str, label: str) -> dict[str, object]:
    start, end = bounds(label)
    chosen = [
        trade for trade in trades
        if start <= datetime.fromtimestamp(trade.entry_ms / 1000, tz=UTC).date() <= end
    ]
    values = [float(getattr(trade, field)) for trade in chosen]
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for value in values:
        equity *= max(0.0, 1.0 + value)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    sigma = pstdev(values) if len(values) > 1 else 0.0
    symbols = sorted({symbol for trade in chosen for symbol in trade.symbols})
    bull = [float(getattr(t, field)) for t in chosen if t.regime > 0]
    bear = [float(getattr(t, field)) for t in chosen if t.regime < 0]

    def compounded(items: list[float]) -> float:
        result = 1.0
        for item in items:
            result *= max(0.0, 1.0 + item)
        return result - 1.0

    return {
        "active_weeks": len(chosen),
        "bull_weeks": len(bull),
        "bear_weeks": len(bear),
        "total_return": equity - 1.0,
        "annualized_sharpe": 0.0 if sigma == 0 else sqrt(52.0) * mean(values) / sigma,
        "max_drawdown": max_dd,
        "bootstrap_positive_fraction_200": _bootstrap(values),
        "distinct_selected_symbols": len(symbols),
        "bull_return": compounded(bull),
        "bear_return": compounded(bear),
        "avg_week_return": mean(values) if values else 0.0,
    }


def passes(stress: dict[str, dict[str, object]], severe: dict[str, dict[str, object]]) -> bool:
    v, o, so = stress["validation"], stress["oos"], severe["oos"]
    return bool(
        int(v["active_weeks"]) >= 30
        and int(o["active_weeks"]) >= 20
        and float(v["total_return"]) > 0
        and float(v["annualized_sharpe"]) >= 0.75
        and float(v["max_drawdown"]) <= 0.30
        and float(o["total_return"]) > 0
        and float(o["annualized_sharpe"]) >= 0.50
        and float(o["max_drawdown"]) <= 0.30
        and float(o["bootstrap_positive_fraction_200"]) >= 0.70
        and int(v["distinct_selected_symbols"]) >= 12
        and int(o["distinct_selected_symbols"]) >= 8
        and float(so["total_return"]) > 0
    )
