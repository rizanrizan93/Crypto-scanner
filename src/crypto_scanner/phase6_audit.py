from __future__ import annotations

import json
from typing import Any

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient
from crypto_scanner.execution_plan import TestnetExecutionArm
from crypto_scanner.lifecycle import LifecycleState, recover_authoritative_state
from crypto_scanner.position_manager import audit_all_protection
from crypto_scanner.safety import SafetyContract


class Phase6AuditError(RuntimeError):
    """Raised when Phase 6 recovery/protection evidence fails closed."""


_ACTIVE_ALGO_STATUSES = frozenset({"NEW", "PENDING", "WORKING"})
_PROTECTOR_TYPES = frozenset({"STOP_MARKET", "TAKE_PROFIT_MARKET"})


def _protector_exchange_contract_rows(
    payload: object,
) -> tuple[list[dict[str, object]], list[str]]:
    """Validate exchange read-back fields that the normalized protector audit omits.

    The regular protection audit owns cardinality, side, quantity, and reduce-only checks.
    This raw read-back closes the remaining evidence gap: every active STOP/TP2 protector
    must come back from Binance as One-way `positionSide=BOTH` and `workingType=MARK_PRICE`.
    Missing fields fail closed rather than being inferred from the submitted request.
    """
    if not isinstance(payload, list):
        raise Phase6AuditError("open-algo-order raw response must be a JSON array")

    rows: list[dict[str, object]] = []
    blockers: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        order_type = str(item.get("orderType") or item.get("type") or "").upper()
        status = str(item.get("algoStatus") or item.get("status") or "").upper()
        if order_type not in _PROTECTOR_TYPES or status not in _ACTIVE_ALGO_STATUSES:
            continue

        symbol = str(item.get("symbol") or "").upper()
        client_algo_id = str(item.get("clientAlgoId") or "")
        working_type = str(item.get("workingType") or "").upper()
        position_side = str(item.get("positionSide") or "").upper()
        reduce_only = bool(item.get("reduceOnly", False))
        contract_ok = (
            bool(symbol)
            and bool(client_algo_id)
            and working_type == "MARK_PRICE"
            and position_side == "BOTH"
            and reduce_only
        )
        rows.append(
            {
                "symbol": symbol,
                "client_algo_id": client_algo_id,
                "order_type": order_type,
                "working_type": working_type,
                "position_side": position_side,
                "reduce_only": reduce_only,
                "contract_ok": contract_ok,
            }
        )
        if not contract_ok:
            blockers.append(
                "PROTECTOR_EXCHANGE_CONTRACT_INVALID:"
                f"{symbol or '*'}:{client_algo_id or '*'}:{order_type}"
            )
    return rows, blockers


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

        # Read the raw venue response as evidence for fields intentionally not normalized
        # into AlgoOrderSnapshot. This keeps MARK_PRICE/BOTH verification authoritative.
        raw_algo_payload: Any = client._signed_get(  # noqa: SLF001
            "/fapi/v1/openAlgoOrders",
            {"algoType": "CONDITIONAL"},
        )
        protector_contract, protector_contract_blockers = _protector_exchange_contract_rows(
            raw_algo_payload
        )

        fill_counts: dict[str, int] = {}
        for position in snapshot.open_positions:
            fill_counts[position.symbol] = len(client.get_user_trades(position.symbol, limit=100))

    blocking_issues = [issue for issue in issues if issue.severity == "BLOCK"]
    blocking_protection = [report for report in protection if report.block_new_entries]
    status = (
        "PASS_PHASE6_READONLY"
        if not blocking_issues
        and not blocking_protection
        and not protector_contract_blockers
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
        "protector_exchange_contract": protector_contract,
        "protector_exchange_contract_blockers": protector_contract_blockers,
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
