from __future__ import annotations

import json

from crypto_scanner.binance_public_archive import (
    BinanceArchiveError,
    BinancePublicArchiveClient,
    make_monthly_package,
)
from crypto_scanner.market_neutral_research import (
    FIXED_UNIVERSE,
    FROZEN_CANDIDATES,
    candidate_payload,
    candles_to_complete_utc_days,
    simulate,
    split_calendar,
    summarize,
)

INTERVAL = "4h"
START_YEAR = 2023
END_YEAR = 2026
END_MONTH = 8
BASE_ROUND_TRIP_BPS = 8.0
STRESS_ROUND_TRIP_BPS = 14.0
BASE_CARRY_BPS_PER_DAY = 1.0
STRESS_CARRY_BPS_PER_DAY = 3.0


def _months():
    for year in range(START_YEAR, END_YEAR + 1):
        last_month = END_MONTH if year == END_YEAR else 12
        for month in range(1, last_month + 1):
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
    if any(
        later.start_time_ms <= earlier.start_time_ms
        for earlier, later in zip(rows, rows[1:], strict=False)
    ):
        raise ValueError(f"non-increasing or duplicate history for {symbol}")
    return tuple(rows), unavailable


def _pass_gate(record: dict[str, object]) -> bool:
    stress = record["stress"]
    assert isinstance(stress, dict)
    train = stress["train"]
    validation = stress["validation"]
    oos = stress["oos"]
    assert isinstance(train, dict)
    assert isinstance(validation, dict)
    assert isinstance(oos, dict)
    return bool(
        int(train["days"]) >= 300
        and int(validation["days"]) >= 300
        and int(oos["days"]) >= 200
        and float(validation["total_return"]) > 0.0
        and float(validation["annualized_sharpe"]) >= 0.75
        and float(validation["max_drawdown"]) <= 0.30
        and float(validation["avg_gross_exposure"]) >= 0.75
        and float(oos["total_return"]) > 0.0
        and float(oos["annualized_sharpe"]) >= 0.50
        and float(oos["max_drawdown"]) <= 0.30
        and float(oos["avg_gross_exposure"]) >= 0.75
        and float(oos["bootstrap_positive_fraction_200"]) >= 0.70
    )


def build_report(history_by_symbol, unavailable_by_symbol):
    daily = {
        symbol: candles_to_complete_utc_days(history_by_symbol[symbol])
        for symbol in FIXED_UNIVERSE
    }
    records = []
    for candidate in FROZEN_CANDIDATES:
        portfolio = simulate(
            daily,
            candidate,
            base_round_trip_bps=BASE_ROUND_TRIP_BPS,
            stress_round_trip_bps=STRESS_ROUND_TRIP_BPS,
            base_carry_bps_per_day=BASE_CARRY_BPS_PER_DAY,
            stress_carry_bps_per_day=STRESS_CARRY_BPS_PER_DAY,
        )
        split = split_calendar(portfolio)
        record = {
            "candidate": candidate.name,
            "parameters": candidate_payload(candidate),
            "base": {
                label: summarize(rows, field="base_return")
                for label, rows in split.items()
            },
            "stress": {
                label: summarize(rows, field="stress_return")
                for label, rows in split.items()
            },
        }
        record["historical_pass"] = _pass_gate(record)
        records.append(record)

    ranked = sorted(
        records,
        key=lambda row: (
            float(row["stress"]["validation"]["annualized_sharpe"]),
            float(row["stress"]["validation"]["total_return"]),
            -float(row["stress"]["validation"]["max_drawdown"]),
        ),
        reverse=True,
    )
    return {
        "schema_version": "CRYPTO_MARKET_NEUTRAL_RELATIVE_VALUE_V1",
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "source": "BINANCE_PUBLIC_ARCHIVE_USDM_4H",
        "universe_contract": "FIXED_20_PAIR_WITH_XLM_NO_FIL",
        "universe": list(FIXED_UNIVERSE),
        "selection_partition": "validation",
        "oos_used_for_selection": False,
        "period": {
            "warmup": "2023",
            "train": "2024",
            "validation": "2025",
            "oos": "2026-01..2026-08",
        },
        "portfolio_contract": {
            "gross_exposure_target": 1.0,
            "net_exposure_target": 0.0,
            "long_book_target": 0.5,
            "short_book_target": -0.5,
            "entry": "next UTC day open after ranking signal",
            "mark_to_market": "next UTC day open",
        },
        "cost_contract": {
            "base_round_trip_bps": BASE_ROUND_TRIP_BPS,
            "stress_round_trip_bps": STRESS_ROUND_TRIP_BPS,
            "base_carry_bps_per_day_on_gross": BASE_CARRY_BPS_PER_DAY,
            "stress_carry_bps_per_day_on_gross": STRESS_CARRY_BPS_PER_DAY,
            "turnover_costing": "half round-trip bps per one-way weight turnover",
        },
        "bias_note": (
            "fixed current 20-pair universe has survivorship bias; any pass is "
            "bounded shadow/Demo eligibility only and requires forward validation"
        ),
        "historical_gate": {
            "train_days_min": 300,
            "validation_days_min": 300,
            "oos_days_min": 200,
            "validation_stress_total_return_min": ">0",
            "validation_stress_sharpe_min": 0.75,
            "validation_stress_max_dd": 0.30,
            "validation_avg_gross_exposure_min": 0.75,
            "oos_stress_total_return_min": ">0",
            "oos_stress_sharpe_min": 0.50,
            "oos_stress_max_dd": 0.30,
            "oos_avg_gross_exposure_min": 0.75,
            "oos_block_bootstrap_positive_fraction_200_min": 0.70,
            "purpose": "research/shadow eligibility only; never direct execution authority",
        },
        "unavailable_months": unavailable_by_symbol,
        "historical_pass": [
            row["candidate"] for row in ranked if row["historical_pass"]
        ],
        "validation_ranking": [row["candidate"] for row in ranked],
        "rows": ranked,
    }


def main() -> int:
    history = {}
    unavailable = {}
    for symbol in FIXED_UNIVERSE:
        candles, missing = fetch_history(symbol)
        history[symbol] = candles
        unavailable[symbol] = missing
    report = build_report(history, unavailable)
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
