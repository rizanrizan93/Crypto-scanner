from __future__ import annotations

import json
from decimal import Decimal

from crypto_scanner.research_tournament import (
    build_tournament_report,
    fetch_candles,
    parse_args,
    validate_split,
)
from crypto_scanner.config import DEFAULT_UNIVERSE


def validation_score(candidate_report: dict[str, object]) -> tuple[Decimal, Decimal, Decimal]:
    partitions = candidate_report["partitions"]
    assert isinstance(partitions, dict)
    base = partitions["base"]
    stress = partitions["stress"]
    assert isinstance(base, dict) and isinstance(stress, dict)
    base_periods = base["periods"]
    stress_periods = stress["periods"]
    assert isinstance(base_periods, dict) and isinstance(stress_periods, dict)
    validation = base_periods["validation"]
    stress_validation = stress_periods["validation"]
    assert isinstance(validation, dict) and isinstance(stress_validation, dict)
    base_summary = validation["summary"]
    stress_summary = stress_validation["summary"]
    breadth = validation["breadth"]
    assert isinstance(base_summary, dict)
    assert isinstance(stress_summary, dict)
    assert isinstance(breadth, dict)
    return (
        Decimal(str(stress_summary["average_net_r"])),
        Decimal(str(base_summary["average_net_r"])),
        Decimal(str(breadth["positive_fraction"])),
    )


def select_on_validation(report: dict[str, object]) -> dict[str, object]:
    candidates = report["candidates"]
    assert isinstance(candidates, list)
    ranked = sorted(candidates, key=validation_score, reverse=True)
    report = dict(report)
    report["schema_version"] = "crypto-research-tournament-v2"
    report["selection_partition"] = "validation"
    report["oos_used_for_selection"] = False
    report["selection_rule"] = (
        "rank by validation stress average_net_r, then validation base average_net_r, "
        "then validation base positive-symbol breadth; OOS is final audit only"
    )
    report["ranking"] = [str(row["candidate"]["name"]) for row in ranked]
    report["candidates"] = ranked
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
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
    raw_report = build_tournament_report(
        candles,
        interval=args.interval,
        start=args.start,
        train_through=args.train_through,
        validation_through=args.validation_through,
        end=args.end,
        base_cost_bps=args.base_cost_bps,
        stress_cost_bps=args.stress_cost_bps,
    )
    report = select_on_validation(raw_report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
