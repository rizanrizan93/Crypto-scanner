from __future__ import annotations

import json
import os
import time
from decimal import Decimal

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient
from crypto_scanner.binance.public_rest import BinanceDemoPublicRestClient
from crypto_scanner.closed_trades import reconstruct_closed_trades
from crypto_scanner.config import load_runtime_config
from crypto_scanner.execution_plan import TestnetExecutionArm
from crypto_scanner.lifecycle import recover_authoritative_state
from crypto_scanner.persistence import (
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseTrajectoryStore,
)
from crypto_scanner.safety import SafetyContract
from crypto_scanner.trade_linkage import DurableTradeLinkage
from crypto_scanner.trajectory import (
    TrajectoryError,
    episode_from_closed_trade,
    infer_open_episode,
    reconstruct_conservative_trajectory,
)
from crypto_scanner.trajectory_store import (
    JsonTrajectoryStore,
    TrajectoryRecord,
    TrajectoryState,
    record_to_dict,
)


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _closed_limit() -> int:
    raw = os.getenv("CRYPTO_SCANNER_PHASE7_CLOSED_LIMIT", "10").strip()
    value = int(raw)
    if not 1 <= value <= 50:
        raise ValueError("CRYPTO_SCANNER_PHASE7_CLOSED_LIMIT must be between 1 and 50")
    return value


def main() -> None:
    safety = SafetyContract()
    safety.validate()
    config = load_runtime_config()
    arm = TestnetExecutionArm.from_environment()
    if arm.enabled:
        raise RuntimeError("Phase 7 audit is read-only and refuses to run while execution is armed")

    persistence_config = SupabasePersistenceConfig.from_environment()
    credentials = BinanceDemoCredentials.from_environment()
    measured_until_ms = time.time_ns() // 1_000_000
    records: list[TrajectoryRecord] = []
    issues: list[dict[str, str]] = []
    linkage = DurableTradeLinkage(persistence_config) if persistence_config.enabled else None

    try:
        with (
            BinanceDemoPrivateReadOnlyClient(credentials) as private,
            BinanceDemoPublicRestClient() as public,
        ):
            lifecycle = recover_authoritative_state(private)
            symbols = set(config.universe) | {
                position.symbol for position in lifecycle.open_positions
            }
            fills_by_symbol = {
                symbol: private.get_user_trades(symbol, limit=1000) for symbol in sorted(symbols)
            }
            income = tuple(
                record
                for symbol in sorted(symbols)
                if fills_by_symbol[symbol]
                for record in private.get_income_history(symbol=symbol, limit=1000)
            )

            for position in lifecycle.open_positions:
                try:
                    episode = infer_open_episode(position, fills_by_symbol[position.symbol])
                    context = (
                        linkage.resolve_context(
                            symbol=episode.symbol,
                            direction=episode.direction,
                            entry_time_ms=episode.entry_time_ms,
                        )
                        if linkage is not None
                        else None
                    )
                    candles = public.get_klines_window(
                        position.symbol,
                        "1",
                        start_time_ms=episode.entry_time_ms,
                        end_time_ms=measured_until_ms + 1,
                    )
                    current_price = position.mark_price
                    if current_price is None or current_price <= 0:
                        current_price = public.get_ticker(position.symbol).mark_price

                    initial_stop = context.initial_stop_loss if context is not None else None
                    eligible = context.calibration_eligible if context is not None else False
                    metrics = reconstruct_conservative_trajectory(
                        episode,
                        candles,
                        measured_until_ms=measured_until_ms,
                        current_price=current_price,
                        initial_stop_loss=initial_stop,
                    )
                    if eligible:
                        note = (
                            "Open trajectory linked to durable scanner signal, regime and original "
                            "geometry; R metrics are calibration eligible."
                        )
                    elif initial_stop is not None:
                        note = (
                            "Open trajectory has durable initial-stop evidence but the full "
                            "scanner signal/setup/regime chain is incomplete; R is measured but "
                            "excluded from calibration."
                        )
                    else:
                        note = (
                            "Open trajectory reconstructed from Binance evidence without a "
                            "complete durable original signal/stop chain; R and calibration "
                            "eligibility are disabled."
                        )
                    records.append(
                        TrajectoryRecord(
                            snapshot=metrics,
                            state=TrajectoryState.OPEN,
                            calibration_eligible=eligible,
                            persistence_mode=(
                                "SUPABASE" if persistence_config.enabled else "NO_SUPABASE"
                            ),
                            note=note,
                            signal_id=context.signal_id if context is not None else None,
                        )
                    )
                except (PersistenceError, TrajectoryError, ValueError, RuntimeError) as exc:
                    issues.append(
                        {
                            "scope": "OPEN",
                            "symbol": position.symbol,
                            "detail": str(exc),
                        }
                    )

            try:
                all_fills = tuple(
                    fill
                    for symbol in sorted(symbols)
                    for fill in fills_by_symbol[symbol]
                )
                closed = reconstruct_closed_trades(all_fills, income)
            except (ValueError, RuntimeError) as exc:
                closed = ()
                issues.append(
                    {
                        "scope": "CLOSED",
                        "symbol": "*",
                        "detail": str(exc),
                    }
                )

            for trade in closed[-_closed_limit() :]:
                try:
                    episode = episode_from_closed_trade(trade)
                    context = (
                        linkage.resolve_context(
                            symbol=episode.symbol,
                            direction=episode.direction,
                            entry_time_ms=episode.entry_time_ms,
                        )
                        if linkage is not None
                        else None
                    )
                    candles = public.get_klines_window(
                        trade.symbol,
                        "1",
                        start_time_ms=trade.entry_time_ms,
                        end_time_ms=trade.exit_time_ms + 1,
                    )
                    initial_stop = context.initial_stop_loss if context is not None else None
                    eligible = context.calibration_eligible if context is not None else False
                    metrics = reconstruct_conservative_trajectory(
                        episode,
                        candles,
                        measured_until_ms=trade.exit_time_ms,
                        current_price=trade.average_exit_price,
                        initial_stop_loss=initial_stop,
                    )
                    if eligible:
                        note = (
                            "Closed trajectory linked to durable scanner signal, setup, regime and "
                            "initial geometry; R metrics are calibration eligible."
                        )
                    elif initial_stop is not None:
                        note = (
                            "Closed trajectory has durable initial-stop evidence but lacks the "
                            "full scanner signal chain; R is measured but excluded from "
                            "calibration."
                        )
                    else:
                        note = (
                            "Closed trajectory reconstructed from Binance fills, income and 1m "
                            "candles; R remains unavailable without original durable geometry."
                        )
                    records.append(
                        TrajectoryRecord(
                            snapshot=metrics,
                            state=TrajectoryState.CLOSED,
                            calibration_eligible=eligible,
                            persistence_mode=(
                                "SUPABASE" if persistence_config.enabled else "NO_SUPABASE"
                            ),
                            note=note,
                            signal_id=context.signal_id if context is not None else None,
                            realized_pnl=trade.realized_pnl,
                            commission=trade.commission,
                            funding_fee=trade.funding_fee,
                            net_pnl=trade.net_pnl,
                            exit_time_ms=trade.exit_time_ms,
                            exit_price=trade.average_exit_price,
                        )
                    )
                except (PersistenceError, TrajectoryError, ValueError, RuntimeError) as exc:
                    issues.append(
                        {
                            "scope": "CLOSED",
                            "symbol": trade.symbol,
                            "detail": str(exc),
                        }
                    )
    finally:
        if linkage is not None:
            linkage.close()

    output_path = os.getenv("CRYPTO_SCANNER_PHASE7_OUTPUT", "").strip()
    if output_path:
        JsonTrajectoryStore(output_path).save(tuple(records))

    persistence_error = False
    if persistence_config.enabled:
        try:
            with SupabaseTrajectoryStore(persistence_config) as store:
                store.save(tuple(records))
        except PersistenceError as exc:
            persistence_error = True
            issues.append(
                {
                    "scope": "PERSISTENCE",
                    "symbol": "*",
                    "detail": str(exc),
                }
            )

    open_count = sum(record.state is TrajectoryState.OPEN for record in records)
    closed_count = sum(record.state is TrajectoryState.CLOSED for record in records)
    eligible_count = sum(record.calibration_eligible for record in records)
    if persistence_error:
        status = "FAIL_PHASE7_PERSISTENCE"
    elif issues and not records:
        status = "FAIL_PHASE7_RECONSTRUCTION"
    elif issues:
        status = "PASS_PHASE7_PARTIAL"
    elif records:
        status = "PASS_PHASE7_RECONSTRUCTION"
    else:
        status = "PASS_PHASE7_NO_TRADE_EVIDENCE"

    if persistence_config.enabled and output_path:
        persistence_mode = "SUPABASE+JSON_DIAGNOSTIC"
    elif persistence_config.enabled:
        persistence_mode = "SUPABASE"
    elif output_path:
        persistence_mode = "JSON_DIAGNOSTIC_ONLY"
    else:
        persistence_mode = "NONE"

    payload = {
        "status": status,
        "venue": "BINANCE",
        "environment": "DEMO",
        "execution_armed": False,
        "live_trading_locked": True,
        "supabase_connected": persistence_config.enabled and not persistence_error,
        "persistence": persistence_mode,
        "calibration_eligible": eligible_count > 0,
        "calibration_eligible_count": eligible_count,
        "trajectory_count": len(records),
        "open_trajectory_count": open_count,
        "closed_trajectory_count": closed_count,
        "trajectories": [record_to_dict(record) for record in records],
        "issues": issues,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=_json_default))
    if persistence_error or (issues and not records):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
