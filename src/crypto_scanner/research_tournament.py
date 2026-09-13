from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Iterable

from crypto_scanner.binance.models import Candle
from crypto_scanner.binance_public_archive import BinancePublicArchiveClient, make_monthly_package
from crypto_scanner.config import DEFAULT_UNIVERSE
from crypto_scanner.historical_research import (
    HistoricalResearchTrade,
    replay_impulse_retest_research,
    summarize_historical_research,
)


@dataclass(frozen=True, order=True, slots=True)
class MonthKey:
    year: int
    month: int

    @classmethod
    def parse(cls, value: str) -> MonthKey:
        try:
            year_text, month_text = value.split("-", 1)
            result = cls(int(year_text), int(month_text))
        except (TypeError, ValueError) as exc:
            raise argparse.ArgumentTypeError("month must use YYYY-MM") from exc
        if result.year < 2020 or not 1 <= result.month <= 12:
            raise argparse.ArgumentTypeError("month is outside the supported archive range")
        return result

    def label(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True, slots=True)
class ResearchCandidate:
    name: str
    horizon_bars: int
    impulse_atr: Decimal
    retest_tolerance_atr: Decimal
    stop_buffer_atr: Decimal
    target_r: Decimal


# Small, preregistered challenger set. Do not mutate this registry after looking at OOS results.
FROZEN_CANDIDATES = (
    ResearchCandidate(
        name="IR_BASE_2R",
        horizon_bars=20,
        impulse_atr=Decimal("1.20"),
        retest_tolerance_atr=Decimal("0.30"),
        stop_buffer_atr=Decimal("0.15"),
        target_r=Decimal("2.00"),
    ),
    ResearchCandidate(
        name="IR_FAST_1P5R",
        horizon_bars=12,
        impulse_atr=Decimal("1.00"),
        retest_tolerance_atr=Decimal("0.25"),
        stop_buffer_atr=Decimal("0.10"),
        target_r=Decimal("1.50"),
    ),
    ResearchCandidate(
        name="IR_SELECTIVE_2R",
        horizon_bars=24,
        impulse_atr=Decimal("1.50"),
        retest_tolerance_atr=Decimal("0.25"),
        stop_buffer_atr=Decimal("0.15"),
        target_r=Decimal("2.00"),
    ),
    ResearchCandidate(
        name="IR_WIDE_2P5R",
        horizon_bars=24,
        impulse_atr=Decimal("1.30"),
        retest_tolerance_atr=Decimal("0.40"),
        stop_buffer_atr=Decimal("0.20"),
        target_r=Decimal("2.50"),
    ),
)


def iter_months(start: MonthKey, end: MonthKey) -> tuple[MonthKey, ...]:
    if end < start:
        raise ValueError("end month must not precede start month")
    rows: list[MonthKey] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        rows.append(MonthKey(year, month))
        month += 1
        if month == 13:
            year += 1
            month = 1
    return tuple(rows)


def validate_split(
    *,
    start: MonthKey,
    train_through: MonthKey,
    validation_through: MonthKey,
    end: MonthKey,
) -> None:
    if not start <= train_through < validation_through < end:
        raise ValueError(
            "calendar split must satisfy start <= train_through < validation_through < end"
        )


def partition_for_month(
    month: MonthKey,
    *,
    train_through: MonthKey,
    validation_through: MonthKey,
) -> str:
    if month <= train_through:
        return "train"
    if month <= validation_through:
        return "validation"
    return "oos"


def trade_month(trade: HistoricalResearchTrade) -> MonthKey:
    dt = datetime.fromtimestamp(trade.entry_time_ms / 1000, tz=UTC)
    return MonthKey(dt.year, dt.month)


def split_trades_by_calendar(
    trades: Iterable[HistoricalResearchTrade],
    *,
    start: MonthKey,
    train_through: MonthKey,
    validation_through: MonthKey,
    end: MonthKey,
) -> dict[str, tuple[HistoricalResearchTrade, ...]]:
    validate_split(
        start=start,
        train_through=train_through,
        validation_through=validation_through,
        end=end,
    )
    buckets: dict[str, list[HistoricalResearchTrade]] = {
        "train": [],
        "validation": [],
        "oos": [],
    }
    for trade in sorted(trades, key=lambda row: row.entry_time_ms):
        month = trade_month(trade)
        if month < start or month > end:
            continue
        buckets[
            partition_for_month(
                month,
                train_through=train_through,
                validation_through=validation_through,
            )
        ].append(trade)
    return {key: tuple(value) for key, value in buckets.items()}


def max_drawdown_r(trades: Iterable[HistoricalResearchTrade]) -> Decimal:
    equity = Decimal(0)
    peak = Decimal(0)
    worst = Decimal(0)
    for trade in sorted(trades, key=lambda row: row.entry_time_ms):
        equity += trade.net_result_r
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def _json_decimal(value: object) -> object:
    return str(value) if isinstance(value, Decimal) else value


def summarize_partition(trades: tuple[HistoricalResearchTrade, ...]) -> dict[str, object]:
    summary = summarize_historical_research(trades)
    result = {key: _json_decimal(value) for key, value in asdict(summary).items()}
    result["total_net_r"] = str(sum((row.net_result_r for row in trades), Decimal(0)))
    result["max_drawdown_r"] = str(max_drawdown_r(trades))
    return result


def _reprice_cost(
    trade: HistoricalResearchTrade,
    *,
    round_trip_cost_bps: Decimal,
) -> HistoricalResearchTrade:
    risk = abs(trade.entry_price - trade.stop_loss)
    if risk <= 0:
        raise ValueError("historical trade has non-positive initial risk")
    cost_r = trade.entry_price * round_trip_cost_bps / Decimal("10000") / risk
    return replace(trade, net_result_r=trade.gross_result_r - cost_r)


def apply_cost(
    trades: tuple[HistoricalResearchTrade, ...],
    *,
    round_trip_cost_bps: Decimal,
) -> tuple[HistoricalResearchTrade, ...]:
    if round_trip_cost_bps < 0:
        raise ValueError("round-trip cost must be non-negative")
    return tuple(_reprice_cost(row, round_trip_cost_bps=round_trip_cost_bps) for row in trades)


def _candidate_trades(
    candles: tuple[Candle, ...],
    *,
    symbol: str,
    candidate: ResearchCandidate,
) -> tuple[HistoricalResearchTrade, ...]:
    # Replay gross once. Base and stress costs are applied from immutable geometry afterwards.
    return replay_impulse_retest_research(
        candles,
        symbol=symbol,
        horizon_bars=candidate.horizon_bars,
        impulse_atr=candidate.impulse_atr,
        retest_tolerance_atr=candidate.retest_tolerance_atr,
        stop_buffer_atr=candidate.stop_buffer_atr,
        target_r=candidate.target_r,
        round_trip_cost_bps=Decimal(0),
    )


def _breadth(
    by_symbol: dict[str, tuple[HistoricalResearchTrade, ...]],
) -> dict[str, object]:
    eligible = {
        symbol: rows
        for symbol, rows in by_symbol.items()
        if rows
    }
    positive = [
        symbol
        for symbol, rows in eligible.items()
        if summarize_historical_research(rows).average_net_r > 0
    ]
    count = len(eligible)
    return {
        "eligible_symbols": count,
        "positive_symbols": len(positive),
        "positive_fraction": str(Decimal(len(positive)) / Decimal(count)) if count else "0",
        "positive_symbol_list": sorted(positive),
    }


def build_candidate_report(
    gross_by_symbol: dict[str, tuple[HistoricalResearchTrade, ...]],
    *,
    candidate: ResearchCandidate,
    start: MonthKey,
    train_through: MonthKey,
    validation_through: MonthKey,
    end: MonthKey,
    base_cost_bps: Decimal,
    stress_cost_bps: Decimal,
) -> dict[str, object]:
    report: dict[str, object] = {
        "candidate": {
            key: _json_decimal(value) for key, value in asdict(candidate).items()
        },
        "partitions": {},
    }
    for cost_label, cost in (("base", base_cost_bps), ("stress", stress_cost_bps)):
        cost_symbol_partitions: dict[str, dict[str, tuple[HistoricalResearchTrade, ...]]] = {}
        for symbol, gross_rows in gross_by_symbol.items():
            priced = apply_cost(gross_rows, round_trip_cost_bps=cost)
            cost_symbol_partitions[symbol] = split_trades_by_calendar(
                priced,
                start=start,
                train_through=train_through,
                validation_through=validation_through,
                end=end,
            )

        partitions: dict[str, object] = {}
        for partition in ("train", "validation", "oos"):
            by_symbol = {
                symbol: parts[partition]
                for symbol, parts in cost_symbol_partitions.items()
            }
            pooled = tuple(
                sorted(
                    (trade for rows in by_symbol.values() for trade in rows),
                    key=lambda row: row.entry_time_ms,
                )
            )
            partitions[partition] = {
                "summary": summarize_partition(pooled),
                "breadth": _breadth(by_symbol),
                "by_symbol": {
                    symbol: summarize_partition(rows)
                    for symbol, rows in sorted(by_symbol.items())
                },
            }
        report["partitions"][cost_label] = {
            "round_trip_cost_bps": str(cost),
            "periods": partitions,
        }
    return report


def research_score(candidate_report: dict[str, object]) -> tuple[Decimal, Decimal, Decimal]:
    partitions = candidate_report["partitions"]
    assert isinstance(partitions, dict)
    base = partitions["base"]
    stress = partitions["stress"]
    assert isinstance(base, dict) and isinstance(stress, dict)
    base_periods = base["periods"]
    stress_periods = stress["periods"]
    assert isinstance(base_periods, dict) and isinstance(stress_periods, dict)
    oos = base_periods["oos"]
    stress_oos = stress_periods["oos"]
    assert isinstance(oos, dict) and isinstance(stress_oos, dict)
    base_summary = oos["summary"]
    stress_summary = stress_oos["summary"]
    breadth = oos["breadth"]
    assert isinstance(base_summary, dict)
    assert isinstance(stress_summary, dict)
    assert isinstance(breadth, dict)
    return (
        Decimal(str(stress_summary["average_net_r"])),
        Decimal(str(base_summary["average_net_r"])),
        Decimal(str(breadth["positive_fraction"])),
    )


def build_tournament_report(
    candles_by_symbol: dict[str, tuple[Candle, ...]],
    *,
    interval: str,
    start: MonthKey,
    train_through: MonthKey,
    validation_through: MonthKey,
    end: MonthKey,
    base_cost_bps: Decimal,
    stress_cost_bps: Decimal,
    candidates: tuple[ResearchCandidate, ...] = FROZEN_CANDIDATES,
) -> dict[str, object]:
    validate_split(
        start=start,
        train_through=train_through,
        validation_through=validation_through,
        end=end,
    )
    if stress_cost_bps < base_cost_bps:
        raise ValueError("stress cost must be greater than or equal to base cost")
    if not candles_by_symbol:
        raise ValueError("tournament requires at least one symbol")
    if not candidates:
        raise ValueError("tournament requires at least one candidate")

    candidate_reports: list[dict[str, object]] = []
    for candidate in candidates:
        gross_by_symbol = {
            symbol: _candidate_trades(candles, symbol=symbol, candidate=candidate)
            for symbol, candles in sorted(candles_by_symbol.items())
        }
        candidate_reports.append(
            build_candidate_report(
                gross_by_symbol,
                candidate=candidate,
                start=start,
                train_through=train_through,
                validation_through=validation_through,
                end=end,
                base_cost_bps=base_cost_bps,
                stress_cost_bps=stress_cost_bps,
            )
        )

    ranked = sorted(candidate_reports, key=research_score, reverse=True)
    return {
        "schema_version": "crypto-research-tournament-v1",
        "research_only": True,
        "source": "BINANCE_PUBLIC_ARCHIVE_USDM",
        "forward_demo_evidence_mutated": False,
        "promotion_state_mutated": False,
        "live_execution_enabled": False,
        "selection_rule": (
            "rank by OOS stress average_net_r, then OOS base average_net_r, "
            "then OOS base positive-symbol breadth"
        ),
        "interval": interval,
        "symbols": sorted(candles_by_symbol),
        "period": {
            "start": start.label(),
            "train_through": train_through.label(),
            "validation_through": validation_through.label(),
            "end": end.label(),
        },
        "base_cost_bps": str(base_cost_bps),
        "stress_cost_bps": str(stress_cost_bps),
        "ranking": [str(row["candidate"]["name"]) for row in ranked],
        "candidates": ranked,
    }


def fetch_candles(
    *,
    symbols: tuple[str, ...],
    interval: str,
    start: MonthKey,
    end: MonthKey,
) -> dict[str, tuple[Candle, ...]]:
    months = iter_months(start, end)
    result: dict[str, tuple[Candle, ...]] = {}
    with BinancePublicArchiveClient() as client:
        for symbol in symbols:
            rows: list[Candle] = []
            for month in months:
                package = make_monthly_package(symbol, interval, month.year, month.month)
                rows.extend(client.fetch_month(package))
            if any(
                second.start_time_ms <= first.start_time_ms
                for first, second in zip(rows, rows[1:], strict=False)
            ):
                raise ValueError(f"archive history is not strictly increasing for {symbol}")
            result[symbol] = tuple(rows)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Research-only 20-pair crypto walk-forward tournament."
    )
    parser.add_argument("--symbols", default=",".join(DEFAULT_UNIVERSE))
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--start", type=MonthKey.parse, default=MonthKey(2024, 1))
    parser.add_argument("--train-through", type=MonthKey.parse, default=MonthKey(2024, 12))
    parser.add_argument(
        "--validation-through", type=MonthKey.parse, default=MonthKey(2025, 12)
    )
    parser.add_argument("--end", type=MonthKey.parse, default=MonthKey(2026, 8))
    parser.add_argument("--base-cost-bps", type=Decimal, default=Decimal("8"))
    parser.add_argument("--stress-cost-bps", type=Decimal, default=Decimal("14"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    symbols = tuple(
        item.strip().upper()
        for item in args.symbols.split(",")
        if item.strip()
    )
    if not symbols or any(symbol not in DEFAULT_UNIVERSE for symbol in symbols):
        raise SystemExit("symbols must be a non-empty subset of the fixed 20-pair universe")
    validate_split(
        start=args.start,
        train_through=args.train_through,
        validation_through=args.validation_through,
        end=args.end,
    )
    candles = fetch_candles(
        symbols=symbols,
        interval=args.interval,
        start=args.start,
        end=args.end,
    )
    report = build_tournament_report(
        candles,
        interval=args.interval,
        start=args.start,
        train_through=args.train_through,
        validation_through=args.validation_through,
        end=args.end,
        base_cost_bps=args.base_cost_bps,
        stress_cost_bps=args.stress_cost_bps,
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
