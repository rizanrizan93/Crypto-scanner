from __future__ import annotations

import json
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import sqrt
from random import Random
from statistics import mean, pstdev

from crypto_scanner.bull_bear_regime_data import FundingPoint, load_history

DAY_MS = 86_400_000
SEED = 20260913


@dataclass(frozen=True, slots=True)
class DayResult:
    entry_time_ms: int
    position: int
    price_return: float
    funding_return: float
    base_return: float
    stress_return: float
    severe_return: float
    regime: str


def _ema(values: list[float], span: int) -> list[float]:
    alpha = 2.0 / (span + 1.0)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1.0 - alpha) * output[-1])
    return output


def _regimes(closes: list[float]) -> list[int]:
    ema200 = _ema(closes, 200)
    output = [0] * len(closes)
    for idx in range(199, len(closes)):
        if idx < 60 or closes[idx - 60] <= 0:
            continue
        momentum = closes[idx] / closes[idx - 60] - 1.0
        if closes[idx] > ema200[idx] and momentum > 0:
            output[idx] = 1
        elif closes[idx] < ema200[idx] and momentum < 0:
            output[idx] = -1
    return output


def _funding_sum(
    funding: tuple[FundingPoint, ...],
    times: list[int],
    start_ms: int,
    end_ms: int,
) -> float:
    start = bisect_left(times, start_ms + 1)
    end = bisect_left(times, end_ms)
    return sum(funding[idx].rate for idx in range(start, end))


def simulate() -> tuple[DayResult, ...]:
    bars, funding, missing_price, missing_funding = load_history()
    print("REGIME_COVERAGE", len(bars), len(funding), missing_price, missing_funding)
    closes = [bar.close for bar in bars]
    regimes = _regimes(closes)
    funding_times = [point.time_ms for point in funding]
    output: list[DayResult] = []
    prior_position = 0
    for signal_idx in range(199, len(bars) - 2):
        signal_bar = bars[signal_idx]
        entry_bar = bars[signal_idx + 1]
        exit_bar = bars[signal_idx + 2]
        if entry_bar.start_time_ms - signal_bar.start_time_ms != DAY_MS:
            prior_position = 0
            continue
        if exit_bar.start_time_ms - entry_bar.start_time_ms != DAY_MS:
            prior_position = 0
            continue
        position = regimes[signal_idx]
        if position > 0:
            regime = "BULL_LONG"
        elif position < 0:
            regime = "BEAR_SHORT"
        else:
            regime = "NEUTRAL_FLAT"
        if entry_bar.open <= 0:
            prior_position = position
            continue
        price_return = position * (exit_bar.open / entry_bar.open - 1.0)
        raw_funding = -position * _funding_sum(
            funding,
            funding_times,
            entry_bar.start_time_ms,
            exit_bar.start_time_ms,
        )
        stressed_funding = raw_funding * (0.8 if raw_funding >= 0 else 1.2)
        exposure_change = abs(position - prior_position)
        base_cost = 0.0008 * 0.5 * exposure_change
        stress_cost = 0.0014 * 0.5 * exposure_change
        severe_cost = 0.0025 * 0.5 * exposure_change
        output.append(
            DayResult(
                entry_time_ms=entry_bar.start_time_ms,
                position=position,
                price_return=price_return,
                funding_return=raw_funding,
                base_return=price_return + raw_funding - base_cost,
                stress_return=price_return + stressed_funding - stress_cost,
                severe_return=price_return + stressed_funding - severe_cost,
                regime=regime,
            )
        )
        prior_position = position
    return tuple(output)


def _bounds(label: str) -> tuple[date, date]:
    if label == "train":
        return date(2021, 1, 1), date(2024, 12, 31)
    if label == "validation":
        return date(2025, 1, 1), date(2025, 12, 31)
    if label == "oos":
        return date(2026, 1, 1), date(2026, 8, 31)
    raise ValueError(label)


def _bootstrap(values: list[float]) -> float:
    if not values:
        return 0.0
    rng = Random(SEED)
    n = len(values)
    positive = 0
    for _ in range(500):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            sample.extend(values[(start + j) % n] for j in range(14))
        equity = 1.0
        for value in sample[:n]:
            equity *= max(0.0, 1.0 + value)
        positive += equity > 1.0
    return positive / 500


def summarize(results: tuple[DayResult, ...], field: str, label: str) -> dict[str, float | int]:
    start, end = _bounds(label)
    chosen = [
        row for row in results
        if start <= datetime.fromtimestamp(row.entry_time_ms / 1000, tz=UTC).date() <= end
    ]
    returns = [float(getattr(row, field)) for row in chosen]
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for value in returns:
        equity *= max(0.0, 1.0 + value)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    sigma = pstdev(returns) if len(returns) > 1 else 0.0
    exposure = [row for row in chosen if row.position != 0]
    return {
        "calendar_days": len(chosen),
        "exposure_days": len(exposure),
        "long_days": sum(row.position > 0 for row in chosen),
        "short_days": sum(row.position < 0 for row in chosen),
        "flat_days": sum(row.position == 0 for row in chosen),
        "total_return": equity - 1.0,
        "annualized_sharpe": 0.0 if sigma == 0 else sqrt(365.0) * mean(returns) / sigma,
        "max_drawdown": max_dd,
        "bootstrap_positive_fraction_500": _bootstrap(returns),
        "avg_daily_return": mean(returns) if returns else 0.0,
        "avg_price_return_when_exposed": mean(row.price_return for row in exposure) if exposure else 0.0,
        "avg_funding_return_when_exposed": mean(row.funding_return for row in exposure) if exposure else 0.0,
    }


def passes(record: dict[str, object]) -> bool:
    stress = record["stress"]
    severe = record["severe_25bps"]
    assert isinstance(stress, dict) and isinstance(severe, dict)
    validation = stress["validation"]
    oos = stress["oos"]
    severe_oos = severe["oos"]
    assert isinstance(validation, dict) and isinstance(oos, dict) and isinstance(severe_oos, dict)
    return bool(
        int(validation["exposure_days"]) >= 180
        and int(oos["exposure_days"]) >= 120
        and int(validation["long_days"]) >= 30
        and int(validation["short_days"]) >= 30
        and int(oos["long_days"]) >= 20
        and int(oos["short_days"]) >= 20
        and float(validation["total_return"]) > 0.0
        and float(validation["annualized_sharpe"]) >= 0.75
        and float(validation["max_drawdown"]) <= 0.35
        and float(oos["total_return"]) > 0.0
        and float(oos["annualized_sharpe"]) >= 0.50
        and float(oos["max_drawdown"]) <= 0.35
        and float(oos["bootstrap_positive_fraction_500"]) >= 0.70
        and float(severe_oos["total_return"]) > 0.0
    )


def main() -> int:
    results = simulate()
    record = {
        "candidate": "BTC_EMA200_MOM60_BULL_LONG_BEAR_SHORT",
        "base": {label: summarize(results, "base_return", label) for label in ("train", "validation", "oos")},
        "stress": {label: summarize(results, "stress_return", label) for label in ("train", "validation", "oos")},
        "severe_25bps": {label: summarize(results, "severe_return", label) for label in ("train", "validation", "oos")},
    }
    record["historical_pass"] = passes(record)
    report = {
        "schema_version": "CRYPTO_BULL_LONG_BEAR_SHORT_V1",
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "frozen_before_test": True,
        "direction_contract": "BULL=LONG; BEAR=SHORT; conflicting regime=NO_TRADE",
        "selection_partition": "validation",
        "oos_used_for_selection": False,
        "historical_pass": [record["candidate"]] if record["historical_pass"] else [],
        "rows": [record],
    }
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
