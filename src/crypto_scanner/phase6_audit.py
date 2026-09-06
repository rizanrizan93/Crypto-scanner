from __future__ import annotations

import json

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient
from crypto_scanner.execution_plan import TestnetExecutionArm
from crypto_scanner.lifecycle import LifecycleState, recover_authoritative_state
from crypto_scanner.position_manager import audit_all_protection
from crypto_scanner.safety import SafetyContract


class Phase6AuditError(RuntimeError):
    """Raised when Phase 6 recovery/protection evidence fails closed."""


def run_phase6_audit() -> dict[str, object]:
    arm = TestnetExecutionArm.from_environment()
    if arm.enabled:
        raise Phase6AuditError("Phase 6 audit must run with Testnet execution DISABLED")

    safety = SafetyContract()
    safety.validate()
    credentials = BinanceDemoCredentials.from_environment()
    with BinanceDemoPrivateReadOnlyClient(credentials) as client:
        snapshot = recover_authoritative_state(client)
        state = LifecycleState()
        state.seed_from_recovery(snapshot)
        issues = state.reconcile(snapshot)
        protection = audit_all_protection(snapshot)

        fill_counts: dict[str, int] = {}
        for position in snapshot.open_positions:
            fill_counts[position.symbol] = len(client.get_user_trades(position.symbol, limit=100))

    blocking_issues = [issue for issue in issues if issue.severity == "BLOCK"]
    blocking_protection = [report for report in protection if report.block_new_entries]
    status = (
        "PASS_PHASE6_READONLY"
        if not blocking_issues and not blocking_protection
        else "BLOCKED_PHASE6_READONLY"
    )
    return {
        "status": status,
        "venue": safety.venue.value,
        "environment": "DEMO",
        "live_trading_locked": safety.live_trading_locked,
        "execution_armed": arm.enabled,
        "hedged_mode": snapshot.hedged_mode,
        "equity": str(snapshot.wallet.total_equity),
        "open_positions": [
            {
                "symbol": position.symbol,
                "side": position.side,
                "size": str(position.size),
                "leverage": str(position.leverage),
            }
            for position in snapshot.open_positions
        ],
        "regular_open_orders": len(snapshot.open_orders),
        "algo_open_orders": len(snapshot.open_algo_orders),
        "protection": [
            {
                "symbol": report.symbol,
                "status": report.status.value,
                "block_new_entries": report.block_new_entries,
                "detail": report.detail,
            }
            for report in protection
        ],
        "reconciliation_issues": [
            {
                "severity": issue.severity.value,
                "code": issue.code,
                "symbol": issue.symbol,
                "detail": issue.detail,
            }
            for issue in issues
        ],
        "recent_fill_counts": fill_counts,
    }


def main() -> None:
    report = run_phase6_audit()
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["status"] != "PASS_PHASE6_READONLY":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
