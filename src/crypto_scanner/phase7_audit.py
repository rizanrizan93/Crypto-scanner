from __future__ import annotations

import json
import os
import time
from decimal import Decimal

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient
from crypto_scanner.binance.public_rest import BinanceDemoPublicRestClient
from crypto_scanner.execution_plan import TestnetExecutionArm
from crypto_scanner.lifecycle import recover_authoritative_state
from crypto_scanner.safety import SafetyContract
from crypto_scanner.trajectory import (
    TrajectoryError,
    infer_open_episode,
    reconstruct_conservative_trajectory,
)
from crypto_scanner.trajectory_store import (
    JsonTrajectoryStore,
    TrajectoryRecord,
    record_to_dict,
)


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def main() -> None:
    safety = SafetyContract()
    safety.validate()
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
        for position in lifecycle.open_positions:
            try:
                fills = private.get_user_trades(position.symbol, limit=1000)
                episode = infer_open_episode(position, fills)
                candles = public.get_klines(position.symbol, "1", limit=1500)
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
                    "Diagnostic reconstruction only. Initial signal/stop identity is not yet "
                    "durably linked, so R metrics and calibration eligibility remain disabled."
                )
                records.append(
                    TrajectoryRecord(
                        snapshot=metrics,
                        calibration_eligible=False,
                        persistence_mode="NO_SUPABASE",
                        note=note,
                    )
                )
            except (TrajectoryError, ValueError, RuntimeError) as exc:
                issues.append({"symbol": position.symbol, "detail": str(exc)})

    output_path = os.getenv("CRYPTO_SCANNER_PHASE7_OUTPUT", "").strip()
    if output_path:
        JsonTrajectoryStore(output_path).save(tuple(records))

    if issues and not records:
        status = "FAIL_PHASE7_RECONSTRUCTION"
    elif issues:
        status = "PASS_PHASE7_PARTIAL"
    elif records:
        status = "PASS_PHASE7_RECONSTRUCTION"
    else:
        status = "PASS_PHASE7_NO_OPEN_POSITIONS"

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
        "trajectories": [record_to_dict(record) for record in records],
        "issues": issues,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=_json_default))
    if issues and not records:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
