from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import httpx

from crypto_scanner.binance_public_archive import (
    BinanceArchiveError,
    BinancePublicArchiveClient,
    make_monthly_package,
)
from crypto_scanner.funding_carry_research import (
    FIXED_UNIVERSE,
    FROZEN_CANDIDATES,
    FundingPoint,
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
FUNDING_START = datetime(2023, 1, 1, tzinfo=UTC)
FUNDING_END = datetime(2026, 9, 1, tzinfo=UTC)
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
BASE_ROUND_TRIP_BPS = 8.0
STRESS_ROUND_TRIP_BPS = 14.0
REQUEST_TIMEOUT_SECONDS = 20.0
FUNDING_PAGE_LIMIT = 1000


def _months():
    for year in range(START_YEAR, END_YEAR + 1):
        last_month = END_MONTH if year == END_YEAR else 12
        for month in range(1, last_month + 1):
            yield year, month


def fetch_price_history(symbol: str):
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
        raise ValueError(f"non-increasing or duplicate price history for {symbol}")
    return tuple(rows), unavailable


def fetch_funding_history(symbol: str) -> tuple[FundingPoint, ...]:
    start_ms = int(FUNDING_START.timestamp() * 1000)
    end_ms = int(FUNDING_END.timestamp() * 1000) - 1
    points: list[FundingPoint] = []

    headers = {"User-Agent": "crypto-scanner-research/1.0"}
    with httpx.Client(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=headers,
        follow_redirects=False,
    ) as client:
        cursor = start_ms
        while cursor <= end_ms:
            response = client.get(
                FUNDING_URL,
                params={
                    "symbol": symbol,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": FUNDING_PAGE_LIMIT,
                },
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"BINANCE_FUNDING_HTTP_{response.status_code}:{symbol}"
                )
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError(f"BINANCE_FUNDING_INVALID_PAYLOAD:{symbol}")
            if not payload:
                break

            page: list[FundingPoint] = []
            for item in payload:
                funding_time = int(item["fundingTime"])
                funding_rate = float(item["fundingRate"])
                if funding_time < cursor or funding_time > end_ms:
                    continue
                page.append(
                    FundingPoint(
                        funding_time_ms=funding_time,
                        funding_rate=funding_rate,
                    )
                )
            page.sort(key=lambda point: point.funding_time_ms)
            if not page:
                break
            if any(
                later.funding_time_ms <= earlier.funding_time_ms
                for earlier, later in zip(page, page[1:], strict=False)
            ):
                raise RuntimeError(f"BINANCE_FUNDING_NON_INCREASING_PAGE:{symbol}")
            if points and page[0].funding_time_ms <= points[-1].funding_time_ms:
                raise RuntimeError(f"BINANCE_FUNDING_DUPLICATE_PAGE:{symbol}")
            points.extend(page)
            next_cursor = page[-1].funding_time_ms + 1
            if next_cursor <= cursor:
                raise RuntimeError(f"BINANCE_FUNDING_CURSOR_STALLED:{symbol}")
            cursor = next_cursor
            if len(payload) < FUNDING_PAGE_LIMIT:
                break
            time.sleep(0.05)

    return tuple(points)


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


def build_report(
    price_history_by_symbol,
    funding_by_symbol,
    unavailable_by_symbol,
):
    daily = {
        symbol: candles_to_complete_utc_days(price_history_by_symbol[symbol])
        for symbol in FIXED_UNIVERSE
    }
    records = []
    for candidate in FROZEN_CANDIDATES:
        portfolio = simulate(
            daily,
            funding_by_symbol,
            candidate,
            base_round_trip_bps=BASE_ROUND_TRIP_BPS,
            stress_round_trip_bps=STRESS_ROUND_TRIP_BPS,
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
        "schema_version": "CRYPTO_FUNDING_CARRY_V1",
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "price_source": "BINANCE_PUBLIC_ARCHIVE_USDM_4H",
        "funding_source": "BINANCE_USDM_PUBLIC_FAPI_FUNDING_RATE",
        "funding_endpoint": "/fapi/v1/fundingRate",
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
            "long_book": "most negative trailing funding",
            "short_book": "most positive trailing funding",
            "entry": "next UTC day open after funding ranking signal",
            "exit_mark": "next UTC day open",
            "funding_realization": (
                "only funding timestamps strictly inside entry<time<exit"
            ),
        },
        "cost_contract": {
            "base_round_trip_bps": BASE_ROUND_TRIP_BPS,
            "stress_round_trip_bps": STRESS_ROUND_TRIP_BPS,
            "stress_positive_funding_haircut": 0.20,
            "stress_negative_funding_cost_multiplier": 1.20,
            "turnover_costing": "half round-trip bps per one-way weight turnover",
        },
        "bias_note": (
            "fixed current 20-pair universe has survivorship bias; any pass is "
            "research/shadow eligibility only and requires unseen forward validation"
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
        "coverage": {
            symbol: {
                "price_candles": len(price_history_by_symbol[symbol]),
                "funding_points": len(funding_by_symbol[symbol]),
                "funding_start": (
                    None
                    if not funding_by_symbol[symbol]
                    else funding_by_symbol[symbol][0].timestamp.isoformat()
                ),
                "funding_end": (
                    None
                    if not funding_by_symbol[symbol]
                    else funding_by_symbol[symbol][-1].timestamp.isoformat()
                ),
                "unavailable_price_months": unavailable_by_symbol[symbol],
            }
            for symbol in FIXED_UNIVERSE
        },
        "historical_pass": [
            row["candidate"] for row in ranked if row["historical_pass"]
        ],
        "validation_ranking": [row["candidate"] for row in ranked],
        "rows": ranked,
    }


def main() -> int:
    prices = {}
    funding = {}
    unavailable = {}
    for symbol in FIXED_UNIVERSE:
        candles, missing = fetch_price_history(symbol)
        prices[symbol] = candles
        unavailable[symbol] = missing
        points = fetch_funding_history(symbol)
        funding[symbol] = points
        print(
            f"FUNDING_COVERAGE {symbol} price_candles={len(candles)} "
            f"funding_points={len(points)} missing_price_months={len(missing)}"
        )
    report = build_report(prices, funding, unavailable)
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
