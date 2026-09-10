from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from decimal import Decimal

from crypto_scanner.binance_public_archive import (
    BinancePublicArchiveClient,
    make_monthly_package,
)
from crypto_scanner.historical_research import (
    HistoricalResearchTrade,
    replay_impulse_retest_research,
    summarize_historical_research,
)


def chronological_split(
    trades: tuple[HistoricalResearchTrade, ...],
    *,
    train_fraction: Decimal = Decimal("0.60"),
    validation_fraction: Decimal = Decimal("0.20"),
) -> dict[str, tuple[HistoricalResearchTrade, ...]]:
    if train_fraction <= 0 or validation_fraction <= 0:
        raise ValueError("split fractions must be positive")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("train + validation fractions must leave an OOS partition")

    ordered = tuple(sorted(trades, key=lambda item: item.entry_time_ms))
    count = len(ordered)
    train_end = int(Decimal(count) * train_fraction)
    validation_end = train_end + int(Decimal(count) * validation_fraction)
    return {
        "train": ordered[:train_end],
        "validation": ordered[train_end:validation_end],
        "oos": ordered[validation_end:],
    }


def _summary_dict(trades: tuple[HistoricalResearchTrade, ...]) -> dict[str, object]:
    summary = summarize_historical_research(trades)
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(summary).items()}


def build_report(
    trades: tuple[HistoricalResearchTrade, ...],
    *,
    symbol: str,
    interval: str,
    year: int,
    month: int,
    round_trip_cost_bps: Decimal,
) -> dict[str, object]:
    partitions = chronological_split(trades)
    return {
        "research_only": True,
        "source": "BINANCE_PUBLIC_ARCHIVE_USDM",
        "strategy": "IMPULSE_RETEST_V1",
        "symbol": symbol,
        "interval": interval,
        "year": year,
        "month": month,
        "round_trip_cost_bps_assumption": str(round_trip_cost_bps),
        "sample_count": len(trades),
        "overall": _summary_dict(trades),
        "partitions": {
            name: {
                "sample_count": len(rows),
                "summary": _summary_dict(rows),
            }
            for name, rows in partitions.items()
        },
        "forward_demo_evidence_mutated": False,
        "live_execution_enabled": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run read-only PIT impulse-retest research on a Binance public monthly archive."
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--month", required=True, type=int)
    parser.add_argument("--horizon-bars", type=int, default=20)
    parser.add_argument("--impulse-atr", type=Decimal, default=Decimal("1.20"))
    parser.add_argument("--retest-tolerance-atr", type=Decimal, default=Decimal("0.30"))
    parser.add_argument("--stop-buffer-atr", type=Decimal, default=Decimal("0.15"))
    parser.add_argument("--target-r", type=Decimal, default=Decimal("2.00"))
    parser.add_argument(
        "--round-trip-cost-bps",
        type=Decimal,
        default=Decimal("8"),
        help="Research friction assumption only; not asserted to be the account fee tier.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    package = make_monthly_package(args.symbol, args.interval, args.year, args.month)
    with BinancePublicArchiveClient() as client:
        candles = client.fetch_month(package)

    trades = replay_impulse_retest_research(
        candles,
        horizon_bars=args.horizon_bars,
        stop_buffer_atr=args.stop_buffer_atr,
        target_r=args.target_r,
        impulse_atr=args.impulse_atr,
        retest_tolerance_atr=args.retest_tolerance_atr,
        round_trip_cost_bps=args.round_trip_cost_bps,
    )
    report = build_report(
        trades,
        symbol=package.symbol,
        interval=package.interval,
        year=package.year,
        month=package.month,
        round_trip_cost_bps=args.round_trip_cost_bps,
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
