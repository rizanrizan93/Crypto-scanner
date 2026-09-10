from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from crypto_scanner.binance_public_archive import BinancePublicArchiveClient, make_monthly_package
from crypto_scanner.config import load_runtime_config
from crypto_scanner.historical_research import replay_impulse_retest_research
from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig
from crypto_scanner.strategy_params import StrategyParameters, load_strategy_parameters
from crypto_scanner.strategy_promotion import (
    PromotionStage,
    evaluate_forward_demo_gate,
    evaluate_historical_gate,
    queue_strategy_candidate,
    read_promotion_state,
    record_forward_decision,
    record_historical_decision,
    save_promoted_champion,
    save_promotion_state,
)

DEFAULT_HISTORICAL_MONTHS = 12
DEFAULT_HISTORICAL_SYMBOL_COUNT = 5
DEFAULT_HISTORICAL_INTERVAL = "5m"
DEFAULT_ROUND_TRIP_COST_BPS = Decimal("8")


def _positive_int_environment(name: str, default: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    value = int(raw) if raw else default
    if not 1 <= value <= maximum:
        raise PersistenceError(f"{name} must be between 1 and {maximum}")
    return value


def complete_months(count: int, *, now: datetime | None = None) -> tuple[tuple[int, int], ...]:
    if count < 1:
        raise ValueError("month count must be positive")
    cursor = (now or datetime.now(UTC)).replace(day=1)
    result: list[tuple[int, int]] = []
    year, month = cursor.year, cursor.month - 1
    for _ in range(count):
        if month == 0:
            year, month = year - 1, 12
        result.append((year, month))
        month -= 1
    return tuple(reversed(result))


def robustness_neighbors(params: StrategyParameters) -> tuple[StrategyParameters, ...]:
    candidates = (
        replace(
            params,
            stop_buffer_atr=max(Decimal("0.12"), params.stop_buffer_atr - Decimal("0.02")),
            max_chase_atr=max(Decimal("0.60"), params.max_chase_atr - Decimal("0.05")),
        ),
        replace(
            params,
            stop_buffer_atr=min(Decimal("0.20"), params.stop_buffer_atr + Decimal("0.02")),
            max_chase_atr=min(Decimal("0.80"), params.max_chase_atr + Decimal("0.05")),
        ),
    )
    unique: list[StrategyParameters] = []
    for candidate in candidates:
        candidate.validate()
        if candidate != params and candidate not in unique:
            unique.append(candidate)
    if len(unique) < 2:
        alternate = replace(
            params,
            profit_lock_gap_r=(
                Decimal("0.75")
                if params.profit_lock_gap_r != Decimal("0.75")
                else Decimal("1.25")
            ),
        )
        unique.append(alternate)
    return tuple(unique[:2])


def _historical_result_sets(
    strategies: tuple[StrategyParameters, ...],
    *,
    symbols: tuple[str, ...],
    months: tuple[tuple[int, int], ...],
    interval: str,
    cost_bps: Decimal,
) -> tuple[tuple[Decimal, ...], ...]:
    result_sets: list[list[tuple[int, str, Decimal]]] = [[] for _ in strategies]
    with BinancePublicArchiveClient(timeout_seconds=45.0) as archive:
        for symbol in symbols:
            candles = tuple(
                candle
                for year, month in months
                for candle in archive.fetch_month(
                    make_monthly_package(symbol, interval, year, month)
                )
            )
            for strategy, results in zip(strategies, result_sets, strict=True):
                trades = replay_impulse_retest_research(
                    candles,
                    horizon_bars=20,
                    round_trip_cost_bps=cost_bps,
                    strategy=strategy,
                    symbol=symbol,
                )
                results.extend(
                    (trade.entry_time_ms, symbol, trade.net_result_r) for trade in trades
                )
    for results in result_sets:
        results.sort(key=lambda item: (item[0], item[1]))
    return tuple(tuple(item[2] for item in results) for results in result_sets)


def _rest_rows(
    config: SupabasePersistenceConfig,
    table: str,
    params: dict[str, str],
) -> tuple[dict[str, object], ...]:
    assert config.url is not None and config.service_role_key is not None
    with httpx.Client(timeout=20.0) as client:
        response = client.get(
            f"{config.url.rstrip('/')}/rest/v1/{table}",
            params=params,
            headers={
                "apikey": config.service_role_key,
                "Authorization": f"Bearer {config.service_role_key}",
            },
        )
    if response.is_error:
        raise PersistenceError(
            f"promotion evidence read failed table={table} "
            f"status={response.status_code}: {response.text[:300]}"
        )
    payload = response.json()
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise PersistenceError(f"promotion evidence payload is invalid table={table}")
    return tuple(payload)


def _in_filter(values: tuple[str, ...]) -> str:
    if not values or any(not value.replace("-", "").isalnum() for value in values):
        raise PersistenceError("promotion evidence contains an invalid durable id")
    return "in.(" + ",".join(values) + ")"


def _chunks(values: tuple[str, ...], size: int = 100) -> tuple[tuple[str, ...], ...]:
    return tuple(values[index : index + size] for index in range(0, len(values), size))


def _strategy_signal_ids(
    config: SupabasePersistenceConfig,
    strategy_id: str,
) -> tuple[str, ...]:
    signal_ids: list[str] = []
    for offset in range(0, 10_000, 1000):
        page = _rest_rows(
            config,
            "signals",
            {
                "select": "signal_id",
                "evidence->>strategy_id": f"eq.{strategy_id}",
                "order": "created_at_ms.asc",
                "limit": "1000",
                "offset": str(offset),
            },
        )
        if any(not row.get("signal_id") for row in page):
            raise PersistenceError("strategy signal identity is missing")
        signal_ids.extend(str(row["signal_id"]) for row in page)
        if len(page) < 1000:
            if len(signal_ids) != len(set(signal_ids)):
                raise PersistenceError("strategy signal evidence contains duplicate identities")
            return tuple(signal_ids)
    raise PersistenceError("strategy signal evidence exceeds the bounded 10000-row audit window")


def forward_demo_evidence(
    config: SupabasePersistenceConfig,
    strategy_id: str,
) -> tuple[tuple[Decimal, ...], int, dict[str, object]]:
    signal_ids = _strategy_signal_ids(config, strategy_id)
    if not signal_ids:
        return (), 0, {"strategy_id": strategy_id, "signal_count": 0, "closed_count": 0}
    geometry = tuple(
        row
        for chunk in _chunks(signal_ids)
        for row in _rest_rows(
            config,
            "signal_geometry",
            {"select": "signal_id,stop_loss", "signal_id": _in_filter(chunk), "limit": "100"},
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
            results.append((int(row["exit_time_ms"]), Decimal(str(row["net_pnl"])) / risk))
    results.sort(key=lambda item: item[0])
    duration = results[-1][0] - results[0][0] if len(results) >= 2 else None
    orders = tuple(
        row
        for chunk in _chunks(signal_ids)
        for row in _rest_rows(
            config,
            "orders",
            {"select": "signal_id,status", "signal_id": _in_filter(chunk), "limit": "100"},
        )
    )
    unsafe = {
        str(row.get("status") or "")
        for row in orders
        if "PROTECTION_FAILED" in str(row.get("status") or "")
        or "EMERGENCY_EXIT_FAILED" in str(row.get("status") or "")
    }
    return (
        tuple(value for _, value in results),
        len(unsafe),
        {
            "strategy_id": strategy_id,
            "signal_count": len(signal_ids),
            "closed_count": len(results),
            "evidence_duration_ms": duration,
            "safety_statuses": sorted(unsafe),
        },
    )


def run_strategy_promotion() -> dict[str, object]:
    config = SupabasePersistenceConfig.from_environment()
    if not config.enabled:
        raise PersistenceError("strategy promotion requires dedicated Crypto Scanner Supabase")
    state = read_promotion_state(config)
    if state is None:
        state, _ = queue_strategy_candidate(
            config,
            load_strategy_parameters(config),
            source="BOOTSTRAP_BASELINE",
        )
    if state.stage is PromotionStage.HISTORICAL_PENDING:
        assert state.candidate is not None
        runtime = load_runtime_config()
        month_count = _positive_int_environment(
            "CRYPTO_SCANNER_HISTORICAL_MONTHS", DEFAULT_HISTORICAL_MONTHS, 36
        )
        symbol_count = _positive_int_environment(
            "CRYPTO_SCANNER_HISTORICAL_SYMBOL_COUNT",
            min(DEFAULT_HISTORICAL_SYMBOL_COUNT, len(runtime.universe)),
            len(runtime.universe),
        )
        symbols = runtime.universe[:symbol_count]
        months = complete_months(month_count)
        result_sets = _historical_result_sets(
            (state.candidate.params, *robustness_neighbors(state.candidate.params)),
            symbols=symbols,
            months=months,
            interval=DEFAULT_HISTORICAL_INTERVAL,
            cost_bps=DEFAULT_ROUND_TRIP_COST_BPS,
        )
        results, *neighbors = result_sets
        gate = evaluate_historical_gate(results, robustness_result_sets=tuple(neighbors))
        next_state = record_historical_decision(state, gate)
        next_state = replace(
            next_state,
            historical_evidence={
                **(next_state.historical_evidence or {}),
                "strategy_id": state.candidate.strategy_id,
                "symbols": list(symbols),
                "months": [f"{year:04d}-{month:02d}" for year, month in months],
                "interval": DEFAULT_HISTORICAL_INTERVAL,
                "round_trip_cost_bps": str(DEFAULT_ROUND_TRIP_COST_BPS),
                "lookahead_forbidden": True,
                "intrabar_policy": "SL_FIRST",
                "risk_and_leverage_unchanged": True,
            },
        )
        save_promotion_state(config, next_state)
        return {
            "status": "PASS_HISTORICAL_GATE" if gate.passed else "REJECT_HISTORICAL_GATE",
            "stage": next_state.stage.value,
            "strategy_id": state.candidate.strategy_id,
            "gate": gate.to_dict(),
            "live_trading_locked": True,
        }
    if state.stage is PromotionStage.FORWARD_DEMO:
        assert state.candidate is not None
        results, incidents, evidence = forward_demo_evidence(config, state.candidate.strategy_id)
        duration = evidence.get("evidence_duration_ms")
        gate = evaluate_forward_demo_gate(
            results,
            safety_incident_count=incidents,
            evidence_duration_ms=int(duration) if duration is not None else None,
        )
        next_state = record_forward_decision(state, gate)
        next_state = replace(
            next_state,
            forward_evidence={
                **gate.to_dict(),
                **evidence,
                "environment": "BINANCE_FUTURES_DEMO",
                "live_trading_locked": True,
            },
        )
        if next_state.stage is PromotionStage.PROMOTED:
            save_promoted_champion(config, next_state)
        else:
            save_promotion_state(config, next_state)
        return {
            "status": f"FORWARD_DEMO_{gate.decision}",
            "stage": next_state.stage.value,
            "strategy_id": state.candidate.strategy_id,
            "gate": gate.to_dict(),
            "live_trading_locked": True,
        }
    return {
        "status": "PASS_NO_PROMOTION_ACTION",
        "stage": state.stage.value,
        "champion": asdict(state.champion) if state.champion else None,
        "live_trading_locked": True,
    }


def main() -> None:
    print(json.dumps(run_strategy_promotion(), indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
