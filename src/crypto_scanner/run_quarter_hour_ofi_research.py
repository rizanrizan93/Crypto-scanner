from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from math import sqrt
from random import Random
from statistics import mean, pstdev

from crypto_scanner.quarter_hour_ofi_core import (
    CANDIDATES,
    CORE6,
    Trade,
    build_signals,
    simulate_symbol,
)
from crypto_scanner.quarter_hour_ofi_data import load_symbol

SEED = 20260913
WORKERS = 3


def _bounds(label: str) -> tuple[date, date]:
    if label == "train":
        return date(2024, 1, 1), date(2024, 12, 31)
    if label == "validation":
        return date(2025, 1, 1), date(2025, 12, 31)
    if label == "oos":
        return date(2026, 1, 1), date(2026, 8, 31)
    raise ValueError(label)


def _bootstrap(values: list[float], trials: int = 200, block: int = 14) -> float:
    if not values:
        return 0.0
    rng = Random(SEED)
    positive = 0
    n = len(values)
    for _ in range(trials):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(values[(start + i) % n] for i in range(block))
        equity = 1.0
        for value in sample[:n]:
            equity *= max(0.0, 1.0 + value)
        positive += equity > 1.0
    return positive / trials


def _summary(trades: tuple[Trade, ...], field: str, label: str) -> dict[str, float | int]:
    start, end = _bounds(label)
    chosen = tuple(
        trade
        for trade in trades
        if start
        <= datetime.fromtimestamp(trade.entry_time_ms / 1000, tz=UTC).date()
        <= end
    )
    by_day: dict[date, list[Trade]] = {}
    for trade in chosen:
        day = datetime.fromtimestamp(trade.exit_time_ms / 1000, tz=UTC).date()
        by_day.setdefault(day, []).append(trade)
    sleeve = {symbol: 1.0 / len(CORE6) for symbol in CORE6}
    daily_returns: list[float] = []
    equity_curve = [1.0]
    prior = 1.0
    ruin = 0
    day = start
    while day <= end:
        for trade in sorted(by_day.get(day, []), key=lambda row: row.exit_time_ms):
            result = float(getattr(trade, field))
            if result <= -1.0:
                ruin += 1
            sleeve[trade.symbol] *= max(0.0, 1.0 + result)
        equity = sum(sleeve.values())
        daily_returns.append(0.0 if prior <= 0 else equity / prior - 1.0)
        equity_curve.append(equity)
        prior = equity
        day += timedelta(days=1)
    sigma = pstdev(daily_returns) if len(daily_returns) > 1 else 0.0
    sharpe = 0.0 if sigma == 0 else sqrt(365.0) * mean(daily_returns) / sigma
    peak = equity_curve[0]
    max_dd = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - equity) / peak)
    return {
        "days": len(daily_returns),
        "closed_trades": len(chosen),
        "long_trades": sum(t.direction > 0 for t in chosen),
        "short_trades": sum(t.direction < 0 for t in chosen),
        "total_return": equity_curve[-1] - 1.0,
        "annualized_sharpe": sharpe,
        "max_drawdown": max_dd,
        "bootstrap_positive_fraction_200": _bootstrap(daily_returns),
        "avg_trade_return": mean(float(getattr(t, field)) for t in chosen) if chosen else 0.0,
        "avg_price_return": mean(t.price_return for t in chosen) if chosen else 0.0,
        "avg_funding_return": mean(t.funding_return for t in chosen) if chosen else 0.0,
        "ruin_trades": ruin,
    }


def _pass(record: dict[str, object]) -> bool:
    stress = record["stress"]
    severe = record["severe_25bps"]
    assert isinstance(stress, dict) and isinstance(severe, dict)
    validation = stress["validation"]
    oos = stress["oos"]
    severe_oos = severe["oos"]
    assert isinstance(validation, dict)
    assert isinstance(oos, dict)
    assert isinstance(severe_oos, dict)
    return bool(
        int(validation["closed_trades"]) >= 150
        and int(oos["closed_trades"]) >= 100
        and int(validation["long_trades"]) >= 30
        and int(validation["short_trades"]) >= 30
        and int(oos["long_trades"]) >= 20
        and int(oos["short_trades"]) >= 20
        and int(validation["ruin_trades"]) == 0
        and int(oos["ruin_trades"]) == 0
        and float(validation["total_return"]) > 0.0
        and float(validation["annualized_sharpe"]) >= 0.75
        and float(validation["max_drawdown"]) <= 0.30
        and float(oos["total_return"]) > 0.0
        and float(oos["annualized_sharpe"]) >= 0.50
        and float(oos["max_drawdown"]) <= 0.30
        and float(oos["bootstrap_positive_fraction_200"]) >= 0.70
        and float(severe_oos["total_return"]) > 0.0
    )


def main() -> int:
    market = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(load_symbol, symbol): symbol for symbol in CORE6}
        for future in as_completed(futures):
            symbol, rows, opens, funding = future.result()
            market[symbol] = (rows, opens, funding)
            print("QH_COVERAGE", symbol, len(rows), len(opens), len(funding))

    signals = {symbol: build_signals(market[symbol][0]) for symbol in CORE6}
    records = []
    for candidate in CANDIDATES:
        trades: list[Trade] = []
        for symbol in CORE6:
            _, opens, funding = market[symbol]
            trades.extend(simulate_symbol(symbol, signals[symbol], opens, funding, candidate))
        frozen = tuple(sorted(trades, key=lambda row: (row.entry_time_ms, row.symbol)))
        record = {
            "candidate": candidate.name,
            "hold_hours": candidate.hold_hours,
            "base": {p: _summary(frozen, "base_return", p) for p in ("train", "validation", "oos")},
            "stress": {p: _summary(frozen, "stress_return", p) for p in ("train", "validation", "oos")},
            "severe_25bps": {p: _summary(frozen, "severe_return", p) for p in ("train", "validation", "oos")},
        }
        record["historical_pass"] = _pass(record)
        records.append(record)

    ranked = sorted(
        records,
        key=lambda row: (
            float(row["stress"]["validation"]["annualized_sharpe"]),
            float(row["stress"]["validation"]["total_return"]),
        ),
        reverse=True,
    )
    report = {
        "schema_version": "CRYPTO_QUARTER_HOUR_OFI_V1",
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "frozen_before_test": True,
        "source": "BINANCE_VISION_USDM_MONTHLY_KLINES_1M_PLUS_FUNDING",
        "universe": list(CORE6),
        "direction_contract": "LONG z>=1.5; SHORT z<=-1.5; otherwise NO_TRADE",
        "selection_partition": "validation",
        "oos_used_for_selection": False,
        "signal_counts": {symbol: len(signals[symbol]) for symbol in CORE6},
        "historical_pass": [row["candidate"] for row in ranked if row["historical_pass"]],
        "validation_ranking": [row["candidate"] for row in ranked],
        "rows": ranked,
    }
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
