from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import UTC, datetime
from math import isfinite

import httpx

from crypto_scanner.binance_public_archive import (
    BinanceArchiveError,
    BinancePublicArchiveClient,
    make_monthly_package,
    verify_archive,
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
FUNDING_ARCHIVE_BASE = "https://data.binance.vision/data/futures/um/monthly/fundingRate"
BASE_ROUND_TRIP_BPS = 8.0
STRESS_ROUND_TRIP_BPS = 14.0
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_FUNDING_ARCHIVE_UNCOMPRESSED_BYTES = 64 * 1024 * 1024


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


def _funding_archive_filename(symbol: str, year: int, month: int) -> str:
    return f"{symbol}-fundingRate-{year:04d}-{month:02d}.zip"


def _parse_funding_zip(payload: bytes) -> tuple[FundingPoint, ...]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1:
                raise RuntimeError("funding archive must contain exactly one data file")
            member = members[0]
            if member.file_size > MAX_FUNDING_ARCHIVE_UNCOMPRESSED_BYTES:
                raise RuntimeError("funding archive exceeds uncompressed-size guard")
            raw = archive.read(member).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise RuntimeError("invalid funding archive") from exc

    reader = csv.DictReader(io.StringIO(raw))
    required = {"calc_time", "funding_interval_hours", "last_funding_rate"}
    if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
        raise RuntimeError("funding archive schema mismatch")

    points: list[FundingPoint] = []
    try:
        for row in reader:
            funding_time = int(row["calc_time"])
            interval_hours = int(row["funding_interval_hours"])
            funding_rate = float(row["last_funding_rate"])
            if interval_hours <= 0 or not isfinite(funding_rate):
                raise ValueError("invalid funding row")
            stamp = datetime.fromtimestamp(funding_time / 1000, tz=UTC)
            if not (FUNDING_START <= stamp < FUNDING_END):
                continue
            points.append(
                FundingPoint(
                    funding_time_ms=funding_time,
                    funding_rate=funding_rate,
                )
            )
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("funding archive contains invalid numeric data") from exc

    points.sort(key=lambda point: point.funding_time_ms)
    if any(
        later.funding_time_ms <= earlier.funding_time_ms
        for earlier, later in zip(points, points[1:], strict=False)
    ):
        raise RuntimeError("funding archive timestamps are not strictly increasing")
    return tuple(points)


def fetch_funding_history(symbol: str) -> tuple[tuple[FundingPoint, ...], list[str]]:
    points: list[FundingPoint] = []
    unavailable: list[str] = []
    headers = {"User-Agent": "crypto-scanner-research/1.0"}
    with httpx.Client(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=headers,
        follow_redirects=False,
    ) as client:
        for year, month in _months():
            filename = _funding_archive_filename(symbol, year, month)
            url = f"{FUNDING_ARCHIVE_BASE}/{symbol}/{filename}"
            data_response = client.get(url)
            checksum_response = client.get(f"{url}.CHECKSUM")
            if data_response.status_code == 404 and checksum_response.status_code == 404:
                unavailable.append(f"{year:04d}-{month:02d}")
                continue
            if data_response.status_code != 200 or checksum_response.status_code != 200:
                raise RuntimeError(
                    "BINANCE_VISION_FUNDING_FETCH_FAILED:"
                    f"{symbol}:{year:04d}-{month:02d}:"
                    f"data={data_response.status_code}:"
                    f"checksum={checksum_response.status_code}"
                )
            verify_archive(data_response.content, checksum_response.text, filename)
            points.extend(_parse_funding_zip(data_response.content))

    points.sort(key=lambda point: point.funding_time_ms)
    if any(
        later.funding_time_ms <= earlier.funding_time_ms
        for earlier, later in zip(points, points[1:], strict=False)
    ):
        raise RuntimeError(f"BINANCE_VISION_FUNDING_DUPLICATE_HISTORY:{symbol}")
    return tuple(points), unavailable


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
    unavailable_price_by_symbol,
    unavailable_funding_by_symbol,
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
        "funding_source": "BINANCE_VISION_USDM_MONTHLY_FUNDING_RATE",
        "funding_archive_path": (
            "data/futures/um/monthly/fundingRate/{SYMBOL}/"
            "{SYMBOL}-fundingRate-{YYYY-MM}.zip"
        ),
        "funding_integrity": "sibling .CHECKSUM SHA-256 required; fail closed",
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
                "unavailable_price_months": unavailable_price_by_symbol[symbol],
                "unavailable_funding_months": unavailable_funding_by_symbol[symbol],
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
    unavailable_price = {}
    unavailable_funding = {}
    for symbol in FIXED_UNIVERSE:
        candles, missing_price = fetch_price_history(symbol)
        prices[symbol] = candles
        unavailable_price[symbol] = missing_price
        points, missing_funding = fetch_funding_history(symbol)
        funding[symbol] = points
        unavailable_funding[symbol] = missing_funding
        print(
            f"FUNDING_COVERAGE {symbol} price_candles={len(candles)} "
            f"funding_points={len(points)} "
            f"missing_price_months={len(missing_price)} "
            f"missing_funding_months={len(missing_funding)}"
        )
    report = build_report(
        prices,
        funding,
        unavailable_price,
        unavailable_funding,
    )
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
