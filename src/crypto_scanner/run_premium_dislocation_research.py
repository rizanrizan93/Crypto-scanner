from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from math import sqrt
from random import Random
from statistics import mean, pstdev

from crypto_scanner.premium_dislocation_data import load_symbol
from crypto_scanner.premium_dislocation_engine import CANDIDATES, Trade, simulate_symbol
from crypto_scanner.premium_dislocation_signal import CORE6, build_signals

SEED = 20260913


def bounds(label: str) -> tuple[date, date]:
    mapping = {
        "train": (date(2024, 1, 1), date(2024, 12, 31)),
        "validation": (date(2025, 1, 1), date(2025, 12, 31)),
        "oos": (date(2026, 1, 1), date(2026, 8, 31)),
    }
    return mapping[label]


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
        equity = 1.0
        for value in sample[:n]:
            equity *= max(0.0, 1.0 + value)
        positive += equity > 1.0
    return positive / 200


def summary(trades: tuple[Trade, ...], field: str, label: str) -> dict[str, float | int]:
    start, end = bounds(label)
    chosen = tuple(
        trade for trade in trades
        if start <= datetime.fromtimestamp(trade.entry_time_ms / 1000, tz=UTC).date() <= end
    )
    by_day: dict[date, list[Trade]] = {}
    for trade in chosen:
        exit_day = datetime.fromtimestamp(trade.exit_time_ms / 1000, tz=UTC).date()
        by_day.setdefault(exit_day, []).append(trade)
    sleeves = {symbol: 1.0 / len(CORE6) for symbol in CORE6}
    daily: list[float] = []
    curve = [1.0]
    prior = 1.0
    day = start
    while day <= end:
        for trade in by_day.get(day, []):
            sleeves[trade.symbol] *= max(0.0, 1.0 + float(getattr(trade, field)))
        equity = sum(sleeves.values())
        daily.append(equity / prior - 1.0 if prior > 0 else 0.0)
        curve.append(equity)
        prior = equity
        day += timedelta(days=1)
    sigma = pstdev(daily) if len(daily) > 1 else 0.0
    peak = curve[0]
    drawdown = 0.0
    for equity in curve:
        peak = max(peak, equity)
        drawdown = max(drawdown, 0.0 if peak <= 0 else (peak - equity) / peak)
    return {
        "days": len(daily),
        "closed_trades": len(chosen),
        "long_trades": sum(t.direction > 0 for t in chosen),
        "short_trades": sum(t.direction < 0 for t in chosen),
        "total_return": curve[-1] - 1.0,
        "annualized_sharpe": 0.0 if sigma == 0 else sqrt(365.0) * mean(daily) / sigma,
        "max_drawdown": drawdown,
        "bootstrap_positive_fraction_200": bootstrap(daily),
        "avg_trade_return": mean(float(getattr(t, field)) for t in chosen) if chosen else 0.0,
        "avg_price_return": mean(t.price_return for t in chosen) if chosen else 0.0,
        "avg_funding_return": mean(t.funding_return for t in chosen) if chosen else 0.0,
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
        int(v["closed_trades"]) >= 100 and int(o["closed_trades"]) >= 60
        and int(v["long_trades"]) >= 20 and int(v["short_trades"]) >= 20
        and int(o["long_trades"]) >= 12 and int(o["short_trades"]) >= 12
        and float(v["total_return"]) > 0 and float(v["annualized_sharpe"]) >= 0.75
        and float(v["max_drawdown"]) <= 0.30
        and float(o["total_return"]) > 0 and float(o["annualized_sharpe"]) >= 0.50
        and float(o["max_drawdown"]) <= 0.30
        and float(o["bootstrap_positive_fraction_200"]) >= 0.70
        and float(so["total_return"]) > 0
    )


def main() -> int:
    market = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(load_symbol, symbol): symbol for symbol in CORE6}
        for future in as_completed(futures):
            symbol, premium, opens, funding = future.result()
            market[symbol] = (premium, opens, funding)
            print("PDR_COVERAGE", symbol, len(premium), len(opens), len(funding))
    signals = {
        symbol: build_signals(market[symbol][0], market[symbol][2])
        for symbol in CORE6
    }
    rows = []
    for candidate in CANDIDATES:
        all_trades: list[Trade] = []
        for symbol in CORE6:
            _, opens, funding = market[symbol]
            all_trades.extend(simulate_symbol(symbol, signals[symbol], opens, funding, candidate))
        trades = tuple(sorted(all_trades, key=lambda t: (t.entry_time_ms, t.symbol)))
        record = {
            "candidate": candidate.name,
            "hold_hours": candidate.hold_hours,
            "base": {p: summary(trades, "base_return", p) for p in ("train", "validation", "oos")},
            "stress": {p: summary(trades, "stress_return", p) for p in ("train", "validation", "oos")},
            "severe_25bps": {p: summary(trades, "severe_return", p) for p in ("train", "validation", "oos")},
        }
        record["historical_pass"] = passes(record)
        rows.append(record)
    ranked = sorted(
        rows,
        key=lambda r: (
            float(r["stress"]["validation"]["annualized_sharpe"]),
            float(r["stress"]["validation"]["total_return"]),
        ),
        reverse=True,
    )
    report = {
        "schema_version": "CRYPTO_PREMIUM_DISLOCATION_REVERSAL_V1",
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "frozen_before_test": True,
        "direction_contract": "LONG negative premium dislocation with negative funding; SHORT positive premium dislocation with positive funding",
        "selection_partition": "validation",
        "oos_used_for_selection": False,
        "signal_counts": {symbol: len(signals[symbol]) for symbol in CORE6},
        "historical_pass": [r["candidate"] for r in ranked if r["historical_pass"]],
        "validation_ranking": [r["candidate"] for r in ranked],
        "rows": ranked,
    }
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
