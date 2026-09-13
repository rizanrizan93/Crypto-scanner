from __future__ import annotations

import json
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import sqrt
from random import Random
from statistics import mean, pstdev

from xgboost import XGBRegressor

from crypto_scanner.btc_xgb_data import FundingPoint, load_btc_history
from crypto_scanner.btc_xgb_features import Sample, build_samples

SEED = 20260913


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    threshold: float


CANDIDATES = (
    Candidate("BTC_XGB_H4_COST21", 0.0021),
    Candidate("BTC_XGB_H4_COST38", 0.0038),
)


@dataclass(frozen=True, slots=True)
class Prediction:
    sample: Sample
    forecast: float


@dataclass(frozen=True, slots=True)
class Trade:
    entry_time_ms: int
    exit_time_ms: int
    direction: int
    base_return: float
    stress_return: float
    severe_return: float
    price_return: float
    funding_return: float


def _month_starts(start: date, end: date):
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        yield cursor
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def _next_month(day: date) -> date:
    return date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)


def _model() -> XGBRegressor:
    return XGBRegressor(
        objective="reg:squarederror",
        n_estimators=300,
        max_depth=3,
        learning_rate=0.03,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=20,
        reg_lambda=5.0,
        reg_alpha=0.1,
        random_state=SEED,
        n_jobs=2,
        tree_method="hist",
        verbosity=0,
    )


def walk_forward_predictions(samples: tuple[Sample, ...]) -> tuple[Prediction, ...]:
    predictions: list[Prediction] = []
    eval_start = date(2025, 1, 1)
    eval_end = date(2026, 8, 31)
    for month in _month_starts(eval_start, eval_end):
        start_ms = _ms(month)
        end_ms = _ms(_next_month(month))
        train = [sample for sample in samples if sample.exit_time_ms < start_ms]
        test = [sample for sample in samples if start_ms <= sample.entry_time_ms < end_ms]
        if len(train) < 10_000 or not test:
            continue
        model = _model()
        model.fit(
            [sample.features for sample in train],
            [sample.target_return for sample in train],
        )
        forecasts = model.predict([sample.features for sample in test])
        predictions.extend(
            Prediction(sample=sample, forecast=float(forecast))
            for sample, forecast in zip(test, forecasts, strict=True)
        )
        print("BTC_XGB_MONTH", month.isoformat(), "train", len(train), "test", len(test))
    return tuple(sorted(predictions, key=lambda row: row.sample.entry_time_ms))


def _funding_between(
    funding: tuple[FundingPoint, ...],
    times: list[int],
    entry_ms: int,
    exit_ms: int,
) -> float:
    start = bisect_left(times, entry_ms + 1)
    end = bisect_left(times, exit_ms)
    return sum(funding[idx].rate for idx in range(start, end))


def build_trades(
    predictions: tuple[Prediction, ...],
    funding: tuple[FundingPoint, ...],
    candidate: Candidate,
) -> tuple[Trade, ...]:
    output: list[Trade] = []
    active_until = -1
    times = [point.time_ms for point in funding]
    for row in predictions:
        if row.sample.entry_time_ms < active_until:
            continue
        if row.forecast >= candidate.threshold:
            direction = 1
        elif row.forecast <= -candidate.threshold:
            direction = -1
        else:
            continue
        price_return = direction * row.sample.target_return
        raw_funding = -direction * _funding_between(
            funding,
            times,
            row.sample.entry_time_ms,
            row.sample.exit_time_ms,
        )
        stressed_funding = raw_funding * (0.8 if raw_funding >= 0 else 1.2)
        output.append(
            Trade(
                entry_time_ms=row.sample.entry_time_ms,
                exit_time_ms=row.sample.exit_time_ms,
                direction=direction,
                price_return=price_return,
                funding_return=raw_funding,
                base_return=price_return + raw_funding - 0.0008,
                stress_return=price_return + stressed_funding - 0.0014,
                severe_return=price_return + stressed_funding - 0.0025,
            )
        )
        active_until = row.sample.exit_time_ms
    return tuple(output)


def _bounds(label: str) -> tuple[date, date]:
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


def summarize(trades: tuple[Trade, ...], field: str, label: str) -> dict[str, float | int]:
    start, end = _bounds(label)
    chosen = [
        trade for trade in trades
        if start <= datetime.fromtimestamp(trade.entry_time_ms / 1000, tz=UTC).date() <= end
    ]
    by_day: dict[date, list[Trade]] = {}
    for trade in chosen:
        exit_day = datetime.fromtimestamp(trade.exit_time_ms / 1000, tz=UTC).date()
        by_day.setdefault(exit_day, []).append(trade)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    daily: list[float] = []
    day = start
    while day <= end:
        prior = equity
        for trade in by_day.get(day, []):
            equity *= max(0.0, 1.0 + float(getattr(trade, field)))
        daily.append(0.0 if prior <= 0 else equity / prior - 1.0)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
        day += timedelta(days=1)
    sigma = pstdev(daily) if len(daily) > 1 else 0.0
    return {
        "days": len(daily),
        "closed_trades": len(chosen),
        "long_trades": sum(trade.direction > 0 for trade in chosen),
        "short_trades": sum(trade.direction < 0 for trade in chosen),
        "total_return": equity - 1.0,
        "annualized_sharpe": 0.0 if sigma == 0 else sqrt(365.0) * mean(daily) / sigma,
        "max_drawdown": max_dd,
        "bootstrap_positive_fraction_200": _bootstrap(daily),
        "avg_trade_return": mean(float(getattr(trade, field)) for trade in chosen) if chosen else 0.0,
        "avg_price_return": mean(trade.price_return for trade in chosen) if chosen else 0.0,
        "avg_funding_return": mean(trade.funding_return for trade in chosen) if chosen else 0.0,
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
        int(validation["closed_trades"]) >= 80
        and int(oos["closed_trades"]) >= 50
        and int(validation["long_trades"]) >= 20
        and int(validation["short_trades"]) >= 20
        and int(oos["long_trades"]) >= 12
        and int(oos["short_trades"]) >= 12
        and float(validation["total_return"]) > 0.0
        and float(validation["annualized_sharpe"]) >= 0.75
        and float(validation["max_drawdown"]) <= 0.25
        and float(oos["total_return"]) > 0.0
        and float(oos["annualized_sharpe"]) >= 0.50
        and float(oos["max_drawdown"]) <= 0.25
        and float(oos["bootstrap_positive_fraction_200"]) >= 0.70
        and float(severe_oos["total_return"]) > 0.0
    )


def main() -> int:
    hours, funding, missing_price, missing_funding = load_btc_history()
    print("BTC_XGB_COVERAGE", len(hours), len(funding), missing_price, missing_funding)
    samples = build_samples(hours, funding)
    print("BTC_XGB_SAMPLES", len(samples))
    predictions = walk_forward_predictions(samples)
    print("BTC_XGB_PREDICTIONS", len(predictions))
    records = []
    for candidate in CANDIDATES:
        trades = build_trades(predictions, funding, candidate)
        record = {
            "candidate": candidate.name,
            "threshold_bps": candidate.threshold * 10_000.0,
            "base": {label: summarize(trades, "base_return", label) for label in ("validation", "oos")},
            "stress": {label: summarize(trades, "stress_return", label) for label in ("validation", "oos")},
            "severe_25bps": {label: summarize(trades, "severe_return", label) for label in ("validation", "oos")},
        }
        record["historical_pass"] = passes(record)
        records.append(record)
    ranked = sorted(
        records,
        key=lambda record: (
            float(record["stress"]["validation"]["annualized_sharpe"]),
            float(record["stress"]["validation"]["total_return"]),
        ),
        reverse=True,
    )
    report = {
        "schema_version": "CRYPTO_COST_AWARE_BTC_XGB_V1",
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "frozen_before_test": True,
        "direction_contract": "LONG positive forecast beyond cost threshold; SHORT negative forecast beyond cost threshold; else NO_TRADE",
        "selection_partition": "validation",
        "oos_used_for_selection": False,
        "historical_pass": [row["candidate"] for row in ranked if row["historical_pass"]],
        "validation_ranking": [row["candidate"] for row in ranked],
        "rows": ranked,
    }
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
