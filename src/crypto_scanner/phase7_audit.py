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
from crypto_scanner.safety import SafetyContract
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

    credentials = BinanceDemoCredentials.from_environment()
    measured_until_ms = time.time_ns() // 1_000_000
    records: list[TrajectoryRecord] = []
    issues: list[dict[str, str]] = []

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
                candles = public.get_klines_window(
                    position.symbol,
                    "1",
                    start_time_ms=episode.entry_time_ms,
                    end_time_ms=measured_until_ms + 1,
                )
                current_price = position.mark_price
                if current_price is None or current_price <= 0:
                    current_price = public.get_ticker(position.symbol).mark_price

                metrics = reconstruct_conservative_trajectory(
                    episode,
                    candles,
                    measured_until_ms=measured_until_ms,
                    current_price=current_price,
                    initial_stop_loss=None,
                )
                note = (
                    "Open trajectory reconstructed from Binance evidence. Initial signal/stop "
                    "identity is not durable yet, so R and calibration eligibility are disabled."
                )
                records.append(
                    TrajectoryRecord(
                        snapshot=metrics,
                        state=TrajectoryState.OPEN,
                        calibration_eligible=False,
                        persistence_mode="NO_SUPABASE",
                        note=note,
                    )
                )
            except (TrajectoryError, ValueError, RuntimeError) as exc:
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
                candles = public.get_klines_window(
                    trade.symbol,
                    "1",
                    start_time_ms=trade.entry_time_ms,
                    end_time_ms=trade.exit_time_ms + 1,
                )
                metrics = reconstruct_conservative_trajectory(
                    episode,
                    candles,
                    measured_until_ms=trade.exit_time_ms,
                    current_price=trade.average_exit_price,
                    initial_stop_loss=None,
                )
                note = (
                    "Closed trajectory reconstructed from Binance fills, income and 1m candles. "
                    "R remains unavailable until the original signal stop is durably persisted."
                )
                records.append(
                    TrajectoryRecord(
                        snapshot=metrics,
                        state=TrajectoryState.CLOSED,
                        calibration_eligible=False,
                        persistence_mode="NO_SUPABASE",
                        note=note,
                        realized_pnl=trade.realized_pnl,
                        commission=trade.commission,
                        funding_fee=trade.funding_fee,
                        net_pnl=trade.net_pnl,
                        exit_time_ms=trade.exit_time_ms,
                        exit_price=trade.average_exit_price,
                    )
                )
            except (TrajectoryError, ValueError, RuntimeError) as exc:
                issues.append(
                    {
                        "scope": "CLOSED",
                        "symbol": trade.symbol,
                        "detail": str(exc),
                    }
                )

    output_path = os.getenv("CRYPTO_SCANNER_PHASE7_OUTPUT", "").strip()
    if output_path:
        JsonTrajectoryStore(output_path).save(tuple(records))

    open_count = sum(record.state is TrajectoryState.OPEN for record in records)
    closed_count = sum(record.state is TrajectoryState.CLOSED for record in records)
    if issues and not records:
        status = "FAIL_PHASE7_RECONSTRUCTION"
    elif issues:
        status = "PASS_PHASE7_PARTIAL"
    elif records:
        status = "PASS_PHASE7_RECONSTRUCTION"
    else:
        status = "PASS_PHASE7_NO_TRADE_EVIDENCE"

    payload = {
        "status": status,
        "venue": "BINANCE",
        "environment": "DEMO",
        "execution_armed": False,
        "live_trading_locked": True,
        "supabase_connected": False,
        "persistence": "JSON_DIAGNOSTIC_ONLY" if output_path else "NONE",
        "calibration_eligible": False,
        "trajectory_count": len(records),
        "open_trajectory_count": open_count,
        "closed_trajectory_count": closed_count,
        "trajectories": [record_to_dict(record) for record in records],
        "issues": issues,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=_json_default))
    if issues and not records:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
