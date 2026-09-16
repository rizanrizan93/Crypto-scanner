from __future__ import annotations

import json
import time
from dataclasses import replace
from decimal import Decimal

from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig
from crypto_scanner.strategy_forward_evidence import paper_forward_results
from crypto_scanner.strategy_pool import (
    DemoPoolStatus,
    StrategyPoolEntry,
    StrategyPoolState,
    ensure_strategy_pool,
    save_strategy_pool,
)
from crypto_scanner.strategy_promotion import evaluate_forward_demo_gate
from crypto_scanner.strategy_promotion_runtime import (
    _chunks,
    _in_filter,
    _rest_rows,
    _strategy_signal_ids,
)

MIN_RANKING_SAMPLE = 10


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def forward_demo_evidence_for_stage(
    config: SupabasePersistenceConfig,
    strategy_id: str,
    *,
    promotion_stage: str,
) -> tuple[tuple[Decimal, ...], int, dict[str, object]]:
    signal_ids = _strategy_signal_ids(
        config,
        strategy_id,
        promotion_stage=promotion_stage,
    )
    if not signal_ids:
        return (
            (),
            0,
            {
                "strategy_id": strategy_id,
                "signal_count": 0,
                "closed_count": 0,
                "promotion_stage_filter": promotion_stage,
            },
        )

    geometry = tuple(
        row
        for chunk in _chunks(signal_ids)
        for row in _rest_rows(
            config,
            "signal_geometry",
            {
                "select": "signal_id,stop_loss",
                "signal_id": _in_filter(chunk),
                "limit": "100",
            },
        )
    )
    stops = {
        str(row["signal_id"]): Decimal(str(row["stop_loss"]))
        for row in geometry
        if row.get("signal_id") and row.get("stop_loss") is not None
    }
    closed = tuple(
        row
        for chunk in _chunks(signal_ids)
        for row in _rest_rows(
            config,
            "closed_trades",
            {
                "select": "signal_id,entry_qty,average_entry_price,net_pnl,exit_time_ms",
                "signal_id": _in_filter(chunk),
                "calibration_eligible": "eq.true",
                "history_complete": "eq.true",
                "order": "exit_time_ms.asc",
                "limit": "100",
            },
        )
    )
    results: list[tuple[int, Decimal]] = []
    for row in closed:
        signal_id = str(row.get("signal_id") or "")
        stop = stops.get(signal_id)
        if stop is None:
            continue
        qty = Decimal(str(row["entry_qty"]))
        entry = Decimal(str(row["average_entry_price"]))
        risk = qty * abs(entry - stop)
        if risk > 0:
            results.append(
                (int(row["exit_time_ms"]), Decimal(str(row["net_pnl"])) / risk)
            )
    results.sort(key=lambda item: item[0])
    duration = results[-1][0] - results[0][0] if len(results) >= 2 else None

    orders = tuple(
        row
        for chunk in _chunks(signal_ids)
        for row in _rest_rows(
            config,
            "orders",
            {
                "select": "signal_id,status",
                "signal_id": _in_filter(chunk),
                "limit": "100",
            },
        )
    )
    unsafe_rows = tuple(
        str(row.get("status") or "")
        for row in orders
        if "PROTECTION_FAILED" in str(row.get("status") or "")
        or "EMERGENCY_EXIT_FAILED" in str(row.get("status") or "")
    )
    unsafe_counts = {
        status: unsafe_rows.count(status) for status in sorted(set(unsafe_rows))
    }
    return (
        tuple(value for _, value in results),
        len(unsafe_rows),
        {
            "strategy_id": strategy_id,
            "signal_count": len(signal_ids),
            "closed_count": len(results),
            "evidence_duration_ms": duration,
            "safety_incident_counts": unsafe_counts,
            "promotion_stage_filter": promotion_stage,
        },
    )


def _status_from_decision(
    current: DemoPoolStatus,
    decision: str,
) -> DemoPoolStatus:
    if current in {
        DemoPoolStatus.DEMO_ROLLED_BACK,
        DemoPoolStatus.DEMO_QUARANTINED,
    }:
        return current
    if decision == "QUARANTINE":
        return DemoPoolStatus.DEMO_QUARANTINED
    if decision == "ROLLBACK":
        return DemoPoolStatus.DEMO_ROLLED_BACK
    if decision == "PROMOTE":
        return DemoPoolStatus.DEMO_VALIDATED
    if decision == "WAIT":
        return (
            current
            if current is DemoPoolStatus.DEMO_VALIDATED
            else DemoPoolStatus.FORWARD_DEMO_ACTIVE
        )
    raise PersistenceError(f"unknown forward Demo gate decision: {decision}")


def _decimal_metric(entry: StrategyPoolEntry, key: str, default: str) -> Decimal:
    value = entry.metrics.get(key)
    return Decimal(str(value)) if value is not None else Decimal(default)


def rank_pool_entries(
    entries: tuple[StrategyPoolEntry, ...],
) -> tuple[StrategyPoolEntry, ...]:
    status_order = {
        DemoPoolStatus.DEMO_VALIDATED: 0,
        DemoPoolStatus.FORWARD_DEMO_ACTIVE: 1,
        DemoPoolStatus.DEMO_ROLLED_BACK: 2,
        DemoPoolStatus.DEMO_QUARANTINED: 3,
    }

    def key(entry: StrategyPoolEntry) -> tuple[object, ...]:
        return (
            status_order[entry.status],
            -_decimal_metric(entry, "expectancy_r", "-999999"),
            -_decimal_metric(entry, "profit_factor", "-999999"),
            _decimal_metric(entry, "max_drawdown_r", "999999"),
            -int(entry.metrics.get("sample_size") or 0),
            entry.strategy_id,
        )

    eligible = [
        entry
        for entry in entries
        if int(entry.metrics.get("sample_size") or 0) >= MIN_RANKING_SAMPLE
    ]
    rank_by_id = {
        entry.strategy_id: index
        for index, entry in enumerate(sorted(eligible, key=key), start=1)
    }
    return tuple(
        replace(entry, rank=rank_by_id.get(entry.strategy_id)) for entry in entries
    )


def evaluate_strategy_pool(
    config: SupabasePersistenceConfig,
    *,
    now_ms: int | None = None,
) -> StrategyPoolState:
    timestamp = _now_ms() if now_ms is None else now_ms
    current = ensure_strategy_pool(config, now_ms=timestamp)
    updated: list[StrategyPoolEntry] = []

    for entry in current.entries:
        results, incidents, evidence = forward_demo_evidence_for_stage(
            config,
            entry.strategy_id,
            promotion_stage=entry.evidence_stage_filter,
        )
        duration = evidence.get("evidence_duration_ms")
        gate = evaluate_forward_demo_gate(
            results,
            safety_incident_count=incidents,
            evidence_duration_ms=int(duration) if duration is not None else None,
        )

        paper_results, paper_evidence = paper_forward_results(
            config,
            entry.strategy_id,
            promotion_stage=entry.evidence_stage_filter,
        )
        paper_duration = paper_evidence.get("paper_evidence_duration_ms")
        paper_gate = evaluate_forward_demo_gate(
            paper_results,
            safety_incident_count=0,
            evidence_duration_ms=(
                int(paper_duration) if paper_duration is not None else None
            ),
        )

        # Actual Demo fills remain the only status authority in V1. The paper lane
        # is deliberately wired into the pool as an advisory confirmation gate so
        # it can be compared continuously without creating a second execution path.
        updated.append(
            replace(
                entry,
                status=_status_from_decision(entry.status, gate.decision),
                last_evaluated_ms=timestamp,
                decision=gate.decision,
                reasons=gate.reasons,
                metrics=gate.metrics.to_dict(),
                evidence={
                    **evidence,
                    "environment": "BINANCE_FUTURES_DEMO",
                    "safety_incident_count": incidents,
                    "ranking_min_closed_trades": MIN_RANKING_SAMPLE,
                    "ranking_eligible": gate.metrics.sample_size >= MIN_RANKING_SAMPLE,
                    "paper_forward": {
                        **paper_evidence,
                        "gate_decision": paper_gate.decision,
                        "gate_reasons": list(paper_gate.reasons),
                        "gate_metrics": paper_gate.metrics.to_dict(),
                        "confirmation_ready": paper_gate.decision == "PROMOTE",
                        "status_authority": False,
                    },
                    "paper_confirmation_enabled": True,
                    "paper_status_authority": False,
                    "real_money_trading_enabled": False,
                },
                live_trading_locked=True,
            )
        )

    next_state = StrategyPoolState(
        revision=current.revision + 1,
        updated_at_ms=timestamp,
        entries=rank_pool_entries(tuple(updated)),
        live_trading_locked=True,
    )
    save_strategy_pool(
        config,
        next_state,
        expected_revision=current.revision,
    )
    return next_state


def run_strategy_pool_evaluation() -> dict[str, object]:
    config = SupabasePersistenceConfig.from_environment()
    if not config.enabled:
        raise PersistenceError(
            "multi-strategy Demo pool evaluation requires dedicated Crypto Scanner Supabase"
        )
    state = evaluate_strategy_pool(config)
    return {
        "status": "PASS_MULTI_STRATEGY_DEMO_POOL_EVALUATION",
        "revision": state.revision,
        "strategies": [entry.to_dict() for entry in state.entries],
        "live_trading_locked": True,
        "real_money_trading_enabled": False,
    }


def main() -> None:
    print(
        json.dumps(
            run_strategy_pool_evaluation(),
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
