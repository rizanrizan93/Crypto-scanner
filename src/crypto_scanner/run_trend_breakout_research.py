from __future__ import annotations

import json
from decimal import Decimal

from crypto_scanner.binance_public_archive import (
    BinanceArchiveError,
    BinancePublicArchiveClient,
    make_monthly_package,
)
from crypto_scanner.config import DEFAULT_UNIVERSE
from crypto_scanner.trend_breakout_research import (
    FROZEN_CANDIDATES,
    reprice_cost,
    replay_candidate,
    split_calendar,
    summary_payload,
)

INTERVAL = "1h"
BASE_COST_BPS = Decimal("8")
STRESS_COST_BPS = Decimal("14")


def _months():
    for year in (2024, 2025, 2026):
        last = 8 if year == 2026 else 12
        for month in range(1, last + 1):
            yield year, month


def fetch_history(symbol: str):
    rows = []
    unavailable = []
    with BinancePublicArchiveClient() as client:
        for year, month in _months():
            package = make_monthly_package(symbol, INTERVAL, year, month)
            try:
                rows.extend(client.fetch_month(package))
            except BinanceArchiveError as exc:
                text = str(exc)
                if "data=404 checksum=404" in text:
                    unavailable.append(f"{year:04d}-{month:02d}")
                    continue
                raise
    rows = sorted(rows, key=lambda row: row.start_time_ms)
    if any(b.start_time_ms <= a.start_time_ms for a, b in zip(rows, rows[1:], strict=False)):
        raise ValueError(f"non-increasing or duplicate history for {symbol}")
    return tuple(rows), unavailable


def _periods(gross, cost):
    priced = reprice_cost(gross, round_trip_cost_bps=cost)
    split = split_calendar(priced)
    return {key: summary_payload(value) for key, value in split.items()}


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _historical_pass(row):
    train = row["base"]["train"]
    validation = row["stress"]["validation"]
    oos = row["stress"]["oos"]
    return bool(
        int(train["trades"]) >= 100
        and int(validation["trades"]) >= 30
        and int(oos["trades"]) >= 30
        and _number(validation["expectancy_r"]) >= 0.05
        and _number(validation["profit_factor"]) >= 1.10
        and _number(oos["expectancy_r"]) >= 0.05
        and _number(oos["profit_factor"]) >= 1.10
        and _number(oos["max_drawdown_r"], 999.0) <= 15.0
    )


def build_report(history_by_symbol, unavailable_by_symbol):
    result = []
    for candidate in FROZEN_CANDIDATES:
        for symbol in DEFAULT_UNIVERSE:
            gross = replay_candidate(
                history_by_symbol[symbol],
                symbol=symbol,
                candidate=candidate,
            )
            record = {
                "candidate": candidate.name,
                "symbol": symbol,
                "base": _periods(gross, BASE_COST_BPS),
                "stress": _periods(gross, STRESS_COST_BPS),
                "unavailable_months": unavailable_by_symbol[symbol],
            }
            record["historical_pass"] = _historical_pass(record)
            result.append(record)

    # Validation only determines ordering. OOS remains final audit.
    ranked = sorted(
        result,
        key=lambda row: (
            _number(row["stress"]["validation"]["expectancy_r"]),
            _number(row["stress"]["validation"]["profit_factor"]),
            int(row["stress"]["validation"]["trades"]),
        ),
        reverse=True,
    )
    return {
        "schema_version": "CRYPTO_TREND_BREAKOUT_RESEARCH_V1",
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "source": "BINANCE_PUBLIC_ARCHIVE_USDM",
        "interval": INTERVAL,
        "selection_partition": "validation",
        "oos_used_for_selection": False,
        "period": {"train": "2024", "validation": "2025", "oos": "2026-01..2026-08"},
        "cost_bps": {"base": str(BASE_COST_BPS), "stress": str(STRESS_COST_BPS)},
        "historical_gate": {
            "train_trades_min": 100,
            "validation_trades_min": 30,
            "oos_trades_min": 30,
            "validation_stress_expectancy_r_min": "0.05",
            "validation_stress_pf_min": "1.10",
            "oos_stress_expectancy_r_min": "0.05",
            "oos_stress_pf_min": "1.10",
            "oos_stress_max_dd_r": "15.0",
            "purpose": "eligible for bounded Demo forward use; never LIVE authority",
        },
        "historical_pass": [
            f"{row['symbol']}:{row['candidate']}" for row in ranked if row["historical_pass"]
        ],
        "validation_ranking": [f"{row['symbol']}:{row['candidate']}" for row in ranked],
        "rows": ranked,
    }


def main() -> int:
    history = {}
    unavailable = {}
    for symbol in DEFAULT_UNIVERSE:
        candles, missing = fetch_history(symbol)
        history[symbol] = candles
        unavailable[symbol] = missing
    report = build_report(history, unavailable)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
