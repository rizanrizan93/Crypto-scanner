from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.microstructure import (
    BinanceDemoMicrostructureClient,
    BinanceMicrostructureError,
)
from crypto_scanner.binance.models import InstrumentInfo
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient
from crypto_scanner.binance.private_write import (
    BinanceOrderSubmissionError,
    BinanceTestnetOrderClient,
    UnknownSubmissionOutcome,
)
from crypto_scanner.binance.public_rest import BinanceDemoPublicRestClient
from crypto_scanner.config import load_runtime_config
from crypto_scanner.discovery import DiscoveryResult, DiscoveryStatus
from crypto_scanner.discovery_pipeline import DiscoveryPipeline, MicrostructureSnapshot
from crypto_scanner.durable_execution import (
    DurableExecutionCoordinator,
    DurableExecutionError,
    DurableExecutionResult,
)
from crypto_scanner.execution_plan import ExecutionPlanError, TestnetExecutionArm
from crypto_scanner.fast_lane import (
    FastLaneEvidence,
    ReadinessDecision,
    evaluate_execution_readiness,
)
from crypto_scanner.lifecycle import (
    AuthoritativeLifecycleSnapshot,
    LifecycleState,
    ReconciliationSeverity,
    recover_authoritative_state,
)
from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig
from crypto_scanner.position_manager import audit_all_protection
from crypto_scanner.safety import SafetyContract
from crypto_scanner.trade_linkage import DurableTradeLinkage

_HIGH_CORRELATION_BUCKET = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT"})


class ScannerCycleError(RuntimeError):
    """Raised when the scanner runtime cycle cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class AccountExecutionGate:
    blocked: bool
    reasons: tuple[str, ...]
    open_position_symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DurableReadySignal:
    candidate: DiscoveryResult
    readiness: ReadinessDecision
    signal_id: str
    instrument: InstrumentInfo


@dataclass(frozen=True, slots=True)
class ScannerCycleResult:
    status: str
    venue: str
    environment: str
    live_trading_locked: bool
    run_id: str
    execution_armed: bool
    account_gate: AccountExecutionGate
    discovery_result_count: int
    candidate_count: int
    durable_signal_ids: tuple[str, ...]
    readiness: tuple[dict[str, object], ...]
    microstructure_failures: tuple[dict[str, str], ...]
    execution_skips: tuple[dict[str, str], ...]
    execution_attempted: bool
    orders_submitted: int
    execution_result: DurableExecutionResult | None
    execution_error: str | None


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def evaluate_account_execution_gate(
    snapshot: AuthoritativeLifecycleSnapshot,
    safety: SafetyContract,
) -> AccountExecutionGate:
    state = LifecycleState()
    state.seed_from_recovery(snapshot)
    issues = state.reconcile(snapshot)
    protection = audit_all_protection(snapshot)

    reasons = [
        f"{issue.code}:{issue.symbol or '*'}"
        for issue in issues
        if issue.severity is ReconciliationSeverity.BLOCK
    ]
    reasons.extend(
        f"PROTECTION_{report.status.value}:{report.symbol}"
        for report in protection
        if report.block_new_entries
    )
    if len(snapshot.open_positions) >= safety.max_concurrent_positions:
        reasons.append("MAX_POSITIONS_REACHED")

    return AccountExecutionGate(
        blocked=bool(reasons),
        reasons=tuple(reasons),
        open_position_symbols=tuple(
            sorted(position.symbol for position in snapshot.open_positions)
        ),
    )


def candidate_account_skip_reason(
    symbol: str,
    snapshot: AuthoritativeLifecycleSnapshot,
    safety: SafetyContract,
) -> str | None:
    symbol = symbol.upper()
    open_positions = snapshot.open_positions
    if any(position.symbol == symbol for position in open_positions):
        return "POSITION_ALREADY_OPEN"
    if len(open_positions) >= safety.max_concurrent_positions:
        return "MAX_POSITIONS_REACHED"
    if symbol in _HIGH_CORRELATION_BUCKET:
        correlated = sum(
            position.symbol in _HIGH_CORRELATION_BUCKET for position in open_positions
        )
        if correlated >= 2:
            return "HIGH_CORRELATION_BUCKET_FULL"
    return None


def _readiness_row(
    candidate: DiscoveryResult,
    decision: ReadinessDecision,
    *,
    signal_id: str | None,
    orderbook_imbalance: object,
    taker_pressure: object,
) -> dict[str, object]:
    return {
        "symbol": candidate.symbol,
        "status": decision.status.value,
        "signal_id": signal_id,
        "reasons": list(decision.reasons),
        "orderbook_imbalance": str(orderbook_imbalance),
        "taker_pressure": str(taker_pressure),
    }


def _execution_dict(result: DurableExecutionResult | None) -> object:
    if result is None:
        return None
    payload = asdict(result)
    for key, value in tuple(payload.items()):
        if hasattr(value, "as_tuple"):
            payload[key] = str(value)
    return payload


def run_scanner_cycle() -> ScannerCycleResult:
    safety = SafetyContract()
    safety.validate()
    arm = TestnetExecutionArm.from_environment()
    config = load_runtime_config()
    persistence_config = SupabasePersistenceConfig.from_environment()
    if not persistence_config.enabled:
        raise ScannerCycleError("scanner cycle requires dedicated Crypto Scanner Supabase")
    credentials = BinanceDemoCredentials.from_environment()

    micro_failures: list[dict[str, str]] = []
    readiness_rows: list[dict[str, object]] = []
    ready_signals: list[DurableReadySignal] = []
    execution_skips: list[dict[str, str]] = []
    execution_result: DurableExecutionResult | None = None
    execution_error: str | None = None
    execution_attempted = False
    orders_submitted = 0

    with (
        BinanceDemoPublicRestClient(base_url=config.binance_rest_url) as public,
        BinanceDemoMicrostructureClient(base_url=config.binance_rest_url) as micro,
        BinanceDemoPrivateReadOnlyClient(
            credentials,
            base_url=config.binance_rest_url,
        ) as private,
        DurableTradeLinkage(persistence_config) as linkage,
    ):
        snapshot = recover_authoritative_state(private)
        account_gate = evaluate_account_execution_gate(snapshot, safety)

        discovery_micro: dict[str, MicrostructureSnapshot] = {}
        for symbol in config.universe:
            try:
                evidence = micro.get_evidence(symbol)
                discovery_micro[symbol] = MicrostructureSnapshot(
                    orderbook_imbalance=evidence.orderbook_imbalance,
                    taker_pressure=evidence.taker_pressure,
                )
            except (BinanceMicrostructureError, ValueError, RuntimeError) as exc:
                micro_failures.append({"symbol": symbol, "detail": str(exc)})

        run = DiscoveryPipeline(public, universe=config.universe).run(discovery_micro)
        run_id = linkage.save_discovery_run(run, execution_armed=arm.enabled)

        for candidate in run.results:
            if candidate.status is not DiscoveryStatus.CANDIDATE:
                continue
            try:
                fresh = micro.get_evidence(candidate.symbol)
                ticker = public.get_ticker(candidate.symbol)
                quote_timestamp_ms = _now_ms()
                instrument = public.get_instrument(candidate.symbol)
                candles_3m = public.get_klines(candidate.symbol, "3", limit=200)
                candles_5m = public.get_klines(candidate.symbol, "5", limit=200)
                now_ms = _now_ms()
                decision = evaluate_execution_readiness(
                    candidate,
                    candles_3m=candles_3m,
                    candles_5m=candles_5m,
                    ticker=ticker,
                    instrument=instrument,
                    evidence=FastLaneEvidence(
                        quote_timestamp_ms=quote_timestamp_ms,
                        candidate_timestamp_ms=run.completed_at_ms,
                        orderbook_timestamp_ms=min(fresh.observed_at_ms, now_ms),
                        orderbook_imbalance=fresh.orderbook_imbalance,
                        taker_pressure=fresh.taker_pressure,
                        exchange_healthy=True,
                        orderbook_healthy=True,
                    ),
                    now_ms=now_ms,
                )
                signal_id: str | None = None
                if decision.execution_ready:
                    signal_id = linkage.save_execution_ready_signal(
                        run_id=run_id,
                        candidate=candidate,
                        readiness=decision,
                        candidate_timestamp_ms=run.completed_at_ms,
                        geometry_created_at_ms=now_ms,
                    )
                    ready_signals.append(
                        DurableReadySignal(
                            candidate=candidate,
                            readiness=decision,
                            signal_id=signal_id,
                            instrument=instrument,
                        )
                    )
                readiness_rows.append(
                    _readiness_row(
                        candidate,
                        decision,
                        signal_id=signal_id,
                        orderbook_imbalance=fresh.orderbook_imbalance,
                        taker_pressure=fresh.taker_pressure,
                    )
                )
            except (PersistenceError, BinanceMicrostructureError, ValueError, RuntimeError) as exc:
                readiness_rows.append(
                    {
                        "symbol": candidate.symbol,
                        "status": "ERROR",
                        "signal_id": None,
                        "reasons": [str(exc)],
                    }
                )

        selected: DurableReadySignal | None = None
        if arm.enabled and not account_gate.blocked:
            for ready in ready_signals:
                skip = candidate_account_skip_reason(
                    ready.candidate.symbol,
                    snapshot,
                    safety,
                )
                if skip is not None:
                    execution_skips.append(
                        {"symbol": ready.candidate.symbol, "reason": skip}
                    )
                    continue
                selected = ready
                break

        # Re-read authoritative account/protection state immediately before the only
        # possible entry submission. A state change during discovery must fail closed.
        if selected is not None:
            fresh_snapshot = recover_authoritative_state(private)
            fresh_gate = evaluate_account_execution_gate(fresh_snapshot, safety)
            account_gate = fresh_gate
            if fresh_gate.blocked:
                execution_skips.append(
                    {
                        "symbol": selected.candidate.symbol,
                        "reason": "ACCOUNT_GATE_CHANGED",
                    }
                )
                selected = None
            else:
                fresh_skip = candidate_account_skip_reason(
                    selected.candidate.symbol,
                    fresh_snapshot,
                    safety,
                )
                if fresh_skip is not None:
                    execution_skips.append(
                        {"symbol": selected.candidate.symbol, "reason": fresh_skip}
                    )
                    selected = None
                snapshot = fresh_snapshot

        if selected is not None:
            execution_attempted = True
            try:
                with BinanceTestnetOrderClient(
                    credentials,
                    arm,
                    base_url=config.binance_rest_url,
                ) as writer:
                    coordinator = DurableExecutionCoordinator(
                        private=private,
                        writer=writer,
                        linkage=linkage,
                        arm=arm,
                        safety=safety,
                    )
                    execution_result = coordinator.execute(
                        selected.readiness,
                        signal_id=selected.signal_id,
                        instrument=selected.instrument,
                    )
                # This means a fully reconciled entry reached durable protected state.
                # Unknown network outcomes remain orders_submitted=0 and are surfaced
                # separately through execution_attempted + execution_error.
                orders_submitted = 1
            except UnknownSubmissionOutcome as exc:
                execution_error = f"UNKNOWN_SUBMISSION_OUTCOME:{exc.client_id}:{exc}"
            except (
                BinanceOrderSubmissionError,
                DurableExecutionError,
                ExecutionPlanError,
                PersistenceError,
                ValueError,
            ) as exc:
                execution_error = f"{type(exc).__name__}:{exc}"

    if execution_error is not None:
        status = "FAIL_EXECUTION_ATTEMPT"
    elif arm.enabled and account_gate.blocked:
        status = "PASS_SCAN_EXECUTION_BLOCKED"
    elif arm.enabled and execution_result is not None:
        status = "PASS_SCANNER_CYCLE_EXECUTED"
    elif arm.enabled:
        status = "PASS_SCANNER_CYCLE_NO_ENTRY"
    else:
        status = "PASS_SCANNER_CYCLE_DISARMED"

    return ScannerCycleResult(
        status=status,
        venue="BINANCE",
        environment="DEMO",
        live_trading_locked=safety.live_trading_locked,
        run_id=run_id,
        execution_armed=arm.enabled,
        account_gate=account_gate,
        discovery_result_count=len(run.results),
        candidate_count=sum(
            result.status is DiscoveryStatus.CANDIDATE for result in run.results
        ),
        durable_signal_ids=tuple(ready.signal_id for ready in ready_signals),
        readiness=tuple(readiness_rows),
        microstructure_failures=tuple(micro_failures),
        execution_skips=tuple(execution_skips),
        execution_attempted=execution_attempted,
        orders_submitted=orders_submitted,
        execution_result=execution_result,
        execution_error=execution_error,
    )


def main() -> None:
    result = run_scanner_cycle()
    payload = asdict(result)
    payload["execution_result"] = _execution_dict(result.execution_result)
    print(json.dumps(payload, indent=2, sort_keys=True))
    if result.status == "FAIL_EXECUTION_ATTEMPT":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
