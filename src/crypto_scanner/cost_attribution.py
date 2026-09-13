from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from crypto_scanner.calibration import _fetch_json_list
from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig, SupabaseRestClient
from crypto_scanner.transaction_costs import BASELINE_ROUND_TRIP_COST_BPS

COST_ATTRIBUTION_STATE_KEY = "research:execution_cost_attribution:v1"
COST_ATTRIBUTION_SCHEMA = "execution-cost-attribution-v1"
MIN_LEARNED_COST_SAMPLES = 30


@dataclass(frozen=True, slots=True)
class CostTradeSample:
    symbol: str
    commission: Decimal
    funding_fee: Decimal
    realized_pnl: Decimal
    net_pnl: Decimal
    gross_return_bps: Decimal
    commission_bps: Decimal
    funding_bps: Decimal
    net_return_bps: Decimal
    adverse_entry_slippage_bps: Decimal | None
    execution_friction_bps: Decimal


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _in_filter(values: tuple[str, ...]) -> str:
    if not values or any(not value.replace("-", "").isalnum() for value in values):
        raise PersistenceError("cost-attribution durable id is invalid")
    return "in.(" + ",".join(values) + ")"


def _eligible_rows(config: SupabasePersistenceConfig) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for offset in range(0, 10_000, 1000):
        page = _fetch_json_list(
            config,
            "closed_trades",
            {
                "select": (
                    "trade_key,signal_id,symbol,direction,entry_qty,average_entry_price,"
                    "realized_pnl,commission,funding_fee,net_pnl,exit_time_ms"
                ),
                "calibration_eligible": "eq.true",
                "history_complete": "eq.true",
                "order": "exit_time_ms.asc",
                "limit": "1000",
                "offset": str(offset),
            },
        )
        if any(not isinstance(row, dict) for row in page):
            raise PersistenceError("cost-attribution closed-trade payload is invalid")
        rows.extend(row for row in page if isinstance(row, dict))
        if len(page) < 1000:
            return tuple(rows)
    raise PersistenceError("cost-attribution evidence exceeds bounded 10000-row window")


def _planned_entries(
    config: SupabasePersistenceConfig,
    signal_ids: tuple[str, ...],
) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for index in range(0, len(signal_ids), 100):
        chunk = signal_ids[index : index + 100]
        if not chunk:
            continue
        payload = _fetch_json_list(
            config,
            "signal_geometry",
            {
                "select": "signal_id,entry_price",
                "signal_id": _in_filter(chunk),
                "limit": "100",
            },
        )
        for row in payload:
            if not isinstance(row, dict):
                raise PersistenceError("cost-attribution geometry payload is invalid")
            signal_id = str(row.get("signal_id") or "")
            price = row.get("entry_price")
            if signal_id and price is not None:
                result[signal_id] = Decimal(str(price))
    return result


def build_cost_samples(
    rows: tuple[dict[str, object], ...],
    planned_entries: dict[str, Decimal],
) -> tuple[CostTradeSample, ...]:
    samples: list[CostTradeSample] = []
    for row in rows:
        signal_id = str(row.get("signal_id") or "")
        symbol = str(row.get("symbol") or "")
        direction = str(row.get("direction") or "")
        required = (
            "entry_qty",
            "average_entry_price",
            "realized_pnl",
            "commission",
            "funding_fee",
            "net_pnl",
        )
        if not signal_id or not symbol or direction not in {"LONG", "SHORT"}:
            raise PersistenceError("eligible closed trade has incomplete durable identity")
        if any(row.get(field) is None for field in required):
            raise PersistenceError("eligible closed trade is missing execution-cost evidence")

        qty = Decimal(str(row["entry_qty"]))
        actual_entry = Decimal(str(row["average_entry_price"]))
        realized = Decimal(str(row["realized_pnl"]))
        commission = Decimal(str(row["commission"]))
        funding = Decimal(str(row["funding_fee"]))
        net = Decimal(str(row["net_pnl"]))
        if qty <= 0 or actual_entry <= 0:
            raise PersistenceError("eligible closed trade has invalid entry economics")

        notional = qty * actual_entry
        planned = planned_entries.get(signal_id)
        slippage: Decimal | None = None
        if planned is not None:
            if planned <= 0:
                raise PersistenceError("planned signal entry price is invalid")
            adverse = actual_entry - planned if direction == "LONG" else planned - actual_entry
            slippage = max(Decimal(0), adverse / planned * Decimal("10000"))

        commission_bps = commission / notional * Decimal("10000")
        friction = max(Decimal(0), commission_bps) + (slippage or Decimal(0))
        samples.append(
            CostTradeSample(
                symbol=symbol,
                commission=commission,
                funding_fee=funding,
                realized_pnl=realized,
                net_pnl=net,
                gross_return_bps=realized / notional * Decimal("10000"),
                commission_bps=commission_bps,
                funding_bps=funding / notional * Decimal("10000"),
                net_return_bps=net / notional * Decimal("10000"),
                adverse_entry_slippage_bps=slippage,
                execution_friction_bps=friction,
            )
        )
    return tuple(samples)


def _mean(values: tuple[Decimal, ...]) -> Decimal | None:
    return sum(values, Decimal(0)) / Decimal(len(values)) if values else None


def _p75(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    index = ((3 * len(ordered) + 3) // 4) - 1
    return ordered[max(0, min(index, len(ordered) - 1))]


def summarize_costs(samples: tuple[CostTradeSample, ...]) -> dict[str, object]:
    commissions = tuple(sample.commission_bps for sample in samples)
    slippage = tuple(
        value
        for sample in samples
        if (value := sample.adverse_entry_slippage_bps) is not None
    )
    friction = tuple(sample.execution_friction_bps for sample in samples)
    gross = tuple(sample.gross_return_bps for sample in samples)
    net = tuple(sample.net_return_bps for sample in samples)
    eligible = len(samples) >= MIN_LEARNED_COST_SAMPLES
    p75_friction = _p75(friction)
    recommended = BASELINE_ROUND_TRIP_COST_BPS
    if eligible and p75_friction is not None:
        recommended = max(recommended, p75_friction)

    return {
        "sample_size": len(samples),
        "slippage_sample_size": len(slippage),
        "total_realized_pnl": sum((sample.realized_pnl for sample in samples), Decimal(0)),
        "total_commission": sum((sample.commission for sample in samples), Decimal(0)),
        "total_funding_fee": sum((sample.funding_fee for sample in samples), Decimal(0)),
        "total_net_pnl": sum((sample.net_pnl for sample in samples), Decimal(0)),
        "avg_gross_return_bps": _mean(gross),
        "avg_net_return_bps": _mean(net),
        "avg_commission_bps": _mean(commissions),
        "median_commission_bps": median(commissions) if commissions else None,
        "p75_commission_bps": _p75(commissions),
        "median_adverse_entry_slippage_bps": median(slippage) if slippage else None,
        "p75_adverse_entry_slippage_bps": _p75(slippage),
        "p75_execution_friction_bps": p75_friction,
        "avg_gross_to_net_drag_bps": _mean(
            tuple(gross_value - net_value for gross_value, net_value in zip(gross, net))
        ),
        "baseline_round_trip_cost_bps": BASELINE_ROUND_TRIP_COST_BPS,
        "learned_cost_activation_eligible": eligible,
        "recommended_round_trip_cost_bps": recommended,
        "minimum_learned_cost_samples": MIN_LEARNED_COST_SAMPLES,
    }


def run_cost_attribution() -> dict[str, object]:
    config = SupabasePersistenceConfig.from_environment()
    if not config.enabled:
        raise PersistenceError("cost attribution requires dedicated Crypto Scanner Supabase")

    rows = _eligible_rows(config)
    signal_ids = tuple(
        dict.fromkeys(str(row.get("signal_id") or "") for row in rows if row.get("signal_id"))
    )
    samples = build_cost_samples(rows, _planned_entries(config, signal_ids))
    overall = summarize_costs(samples)
    by_symbol = {
        symbol: summarize_costs(tuple(sample for sample in samples if sample.symbol == symbol))
        for symbol in sorted({sample.symbol for sample in samples})
    }
    generated_at_ms = _now_ms()
    state = {
        "schema_version": COST_ATTRIBUTION_SCHEMA,
        "generated_at_ms": generated_at_ms,
        "overall": overall,
        "by_symbol": by_symbol,
        "actual_demo_closed_trades_only": True,
        "commission_from_exchange_fills": True,
        "funding_from_exchange_income": True,
        "entry_slippage_vs_signal_geometry": True,
        "gate_mode": "BASELINE_8BPS_ACTIVE_LEARNED_COST_OBSERVE_ONLY",
        "live_trading_locked": True,
    }
    with SupabaseRestClient(config) as rest:
        rest.upsert(
            "runtime_state",
            (
                {
                    "state_key": COST_ATTRIBUTION_STATE_KEY,
                    "version": 1,
                    "state": state,
                    "updated_at_ms": generated_at_ms,
                },
            ),
            on_conflict=("state_key",),
        )
    return {
        "status": "PASS_EXECUTION_COST_ATTRIBUTION",
        "state_key": COST_ATTRIBUTION_STATE_KEY,
        "overall": overall,
        "by_symbol": by_symbol,
        "live_trading_locked": True,
    }


def main() -> None:
    print(json.dumps(run_cost_attribution(), indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
