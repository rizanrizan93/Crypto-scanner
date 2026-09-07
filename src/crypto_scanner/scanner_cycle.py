from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, replace
from decimal import Decimal

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
from crypto_scanner.execution_plan import (
    ExecutionPlanError,
    TestnetExecutionArm,
    build_entry_order_plan,
)
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
from crypto_scanner.position_manager import audit_all_protection, audit_symbol_protection
from crypto_scanner.safety import SafetyContract
from crypto_scanner.stack_execution import ProfitableStackCoordinator, StackExecutionError
from crypto_scanner.stack_recovery import StackRecoveryResult, recover_stack_transactions
from crypto_scanner.stack_store import DurableStackStore
from crypto_scanner.stacking import evaluate_stack_admission
from crypto_scanner.strategy_params import load_strategy_parameters
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
    stack_recovery: dict[str, object]
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
    issues = state.reconcile(snapshot, safety)
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
        reasons.append("MAX_OPEN_SYMBOLS_REACHED")

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
    *,
    risk_slots_in_use: int | None = None,
    correlated_risk_slots_in_use: int | None = None,
) -> str | None:
    symbol = symbol.upper()
    open_positions = snapshot.open_positions
    same_symbol = any(position.symbol == symbol for position in open_positions)
    if same_symbol and not safety.profitable_stacking_enabled:
        return "POSITION_ALREADY_OPEN"

    slots = len(open_positions) if risk_slots_in_use is None else risk_slots_in_use
    if slots >= safety.max_concurrent_positions:
        return "MAX_RISK_SLOTS_REACHED"
    if symbol in _HIGH_CORRELATION_BUCKET:
        correlated = (
            sum(position.symbol in _HIGH_CORRELATION_BUCKET for position in open_positions)
            if correlated_risk_slots_in_use is None
            else correlated_risk_slots_in_use
        )
        if correlated >= safety.max_high_correlation_risk_slots:
            return "HIGH_CORRELATION_RISK_BUCKET_FULL"
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


def _recovery_dict(result: StackRecoveryResult) -> dict[str, object]:
    return {
        "recovered_symbols": list(result.recovered_symbols),
        "cleared_symbols": list(result.cleared_symbols),
        "blockers": list(result.blockers),
    }


def _risk_accounting(
    stack_store: DurableStackStore,
    snapshot: AuthoritativeLifecycleSnapshot,
    safety: SafetyContract,
) -> tuple[int, int, Decimal]:
    equity = snapshot.wallet.total_equity
    if equity is None or equity <= 0:
        raise ScannerCycleError("authoritative equity missing for logical risk accounting")
    return stack_store.risk_accounting(
        snapshot.positions,
        equity=equity,
        safety=safety,
    )


def _stack_skip_reason(
    ready: DurableReadySignal,
    snapshot: AuthoritativeLifecycleSnapshot,
    stack_store: DurableStackStore,
    safety: SafetyContract,
    *,
    risk_slots: int,
    correlated_slots: int,
    portfolio_risk: Decimal,
) -> str | None:
    position = next(
        (
            item
            for item in snapshot.open_positions
            if item.symbol == ready.candidate.symbol
        ),
        None,
    )
    if position is None:
        return None
    state = stack_store.load(position.symbol)
    if state is None or not state.layers or state.position_id is None:
        return "STACK_LEDGER_MISSING"
    signal = stack_store.signal_runtime_record(ready.signal_id)
    protection = audit_symbol_protection(
        position.symbol,
        snapshot.positions,
        snapshot.open_algo_orders,
    )
    equity = snapshot.wallet.total_equity
    if equity is None or equity <= 0:
        return "STACK_EQUITY_INVALID"
    admission = evaluate_stack_admission(
        position=position,
        protection=protection,
        readiness=ready.readiness,
        signal_id=ready.signal_id,
        signal_expires_at_ms=signal.expires_at_ms or 0,
        now_ms=_now_ms(),
        ledger=state.ledger,
        total_risk_slots_in_use=risk_slots,
        correlated_risk_slots_in_use=correlated_slots,
        portfolio_planned_risk=portfolio_risk,
        equity=equity,
        tick_size=ready.instrument.tick_size,
        safety=safety,
    )
    if not admission.allowed:
        return "STACK_" + "+".join(admission.reasons)
    try:
        build_entry_order_plan(
            ready.readiness,
            signal_id=ready.signal_id,
            wallet=snapshot.wallet,
            positions=snapshot.positions,
            instrument=ready.instrument,
            safety=safety,
            allow_same_symbol=True,
            risk_slots_in_use=risk_slots,
            correlated_risk_slots_in_use=correlated_slots,
            portfolio_planned_risk=portfolio_risk,
        )
    except ExecutionPlanError as exc:
        return f"STACK_PLAN_REJECTED:{exc}"
    return None


def run_scanner_cycle() -> ScannerCycleResult:
    safety = SafetyContract()
    safety.validate()
    arm = TestnetExecutionArm.from_environment()
    config = load_runtime_config()
    persistence_config = SupabasePersistenceConfig.from_environment()
    if not persistence_config.enabled:
        raise ScannerCycleError("scanner cycle requires dedicated Crypto Scanner Supabase")
    strategy = load_strategy_parameters(persistence_config)
    credentials = BinanceDemoCredentials.from_environment()

    micro_failures: list[dict[str, str]] = []
    readiness_rows: list[dict[str, object]] = []
    ready_signals: list[DurableReadySignal] = []
    execution_skips: list[dict[str, str]] = []
    execution_result: DurableExecutionResult | None = None
    execution_error: str | None = None
    execution_attempted = False
    orders_submitted = 0
    recovery_result = StackRecoveryResult((), (), ())

    with (
        BinanceDemoPublicRestClient(base_url=config.binance_rest_url) as public,
        BinanceDemoMicrostructureClient(base_url=config.binance_rest_url) as micro,
        BinanceDemoPrivateReadOnlyClient(
            credentials,
            base_url=config.binance_rest_url,
        ) as private,
        BinanceTestnetOrderClient(
            credentials,
            arm,
            base_url=config.binance_rest_url,
        ) as writer,
        DurableTradeLinkage(persistence_config) as linkage,
        DurableStackStore(persistence_config) as stack_store,
    ):
        if arm.enabled:
            recovery_result = recover_stack_transactions(
                private,
                writer,
                stack_store,
                now_ms=_now_ms(),
            )

        snapshot = recover_authoritative_state(private)
        account_gate = evaluate_account_execution_gate(snapshot, safety)
        risk_slots, correlated_slots, portfolio_risk = _risk_accounting(
            stack_store,
            snapshot,
            safety,
        )
        extra_gate_reasons = list(account_gate.reasons)
        if recovery_result.blockers:
            extra_gate_reasons.extend(recovery_result.blockers)
        if risk_slots >= safety.max_concurrent_positions:
            extra_gate_reasons.append("MAX_LOGICAL_RISK_SLOTS_REACHED")
        max_portfolio_risk = (
            (snapshot.wallet.total_equity or Decimal(0))
            * Decimal(str(safety.max_portfolio_risk_fraction))
        )
        if portfolio_risk >= max_portfolio_risk:
            extra_gate_reasons.append("MAX_PORTFOLIO_PLANNED_RISK_REACHED")
        account_gate = replace(
            account_gate,
            blocked=bool(extra_gate_reasons),
            reasons=tuple(dict.fromkeys(extra_gate_reasons)),
        )

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
                    strategy=strategy,
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
        selected_is_stack = False
        if arm.enabled and not account_gate.blocked:
            for ready in ready_signals:
                skip = candidate_account_skip_reason(
                    ready.candidate.symbol,
                    snapshot,
                    safety,
                    risk_slots_in_use=risk_slots,
                    correlated_risk_slots_in_use=correlated_slots,
                )
                if skip is not None:
                    execution_skips.append({"symbol": ready.candidate.symbol, "reason": skip})
                    continue
                same_symbol = any(
                    position.symbol == ready.candidate.symbol
                    for position in snapshot.open_positions
                )
                if same_symbol:
                    stack_skip = _stack_skip_reason(
                        ready,
                        snapshot,
                        stack_store,
                        safety,
                        risk_slots=risk_slots,
                        correlated_slots=correlated_slots,
                        portfolio_risk=portfolio_risk,
                    )
                    if stack_skip is not None:
                        execution_skips.append(
                            {"symbol": ready.candidate.symbol, "reason": stack_skip}
                        )
                        continue
                    selected_is_stack = True
                else:
                    try:
                        build_entry_order_plan(
                            ready.readiness,
                            signal_id=ready.signal_id,
                            wallet=snapshot.wallet,
                            positions=snapshot.positions,
                            instrument=ready.instrument,
                            safety=safety,
                            risk_slots_in_use=risk_slots,
                            correlated_risk_slots_in_use=correlated_slots,
                            portfolio_planned_risk=portfolio_risk,
                        )
                    except ExecutionPlanError as exc:
                        execution_skips.append(
                            {
                                "symbol": ready.candidate.symbol,
                                "reason": f"PLAN_REJECTED:{exc}",
                            }
                        )
                        continue
                selected = ready
                break

        if selected is not None:
            fresh_snapshot = recover_authoritative_state(private)
            fresh_gate = evaluate_account_execution_gate(fresh_snapshot, safety)
            fresh_risk_slots, fresh_correlated_slots, fresh_portfolio_risk = _risk_accounting(
                stack_store,
                fresh_snapshot,
                safety,
            )
            fresh_reasons = list(fresh_gate.reasons)
            if fresh_risk_slots >= safety.max_concurrent_positions:
                fresh_reasons.append("MAX_LOGICAL_RISK_SLOTS_REACHED")
            fresh_gate = replace(
                fresh_gate,
                blocked=bool(fresh_reasons),
                reasons=tuple(dict.fromkeys(fresh_reasons)),
            )
            account_gate = fresh_gate
            if fresh_gate.blocked:
                execution_skips.append(
                    {"symbol": selected.candidate.symbol, "reason": "ACCOUNT_GATE_CHANGED"}
                )
                selected = None
            else:
                fresh_skip = candidate_account_skip_reason(
                    selected.candidate.symbol,
                    fresh_snapshot,
                    safety,
                    risk_slots_in_use=fresh_risk_slots,
                    correlated_risk_slots_in_use=fresh_correlated_slots,
                )
                if fresh_skip is not None:
                    execution_skips.append(
                        {"symbol": selected.candidate.symbol, "reason": fresh_skip}
                    )
                    selected = None
                elif selected_is_stack:
                    stack_skip = _stack_skip_reason(
                        selected,
                        fresh_snapshot,
                        stack_store,
                        safety,
                        risk_slots=fresh_risk_slots,
                        correlated_slots=fresh_correlated_slots,
                        portfolio_risk=fresh_portfolio_risk,
                    )
                    if stack_skip is not None:
                        execution_skips.append(
                            {"symbol": selected.candidate.symbol, "reason": stack_skip}
                        )
                        selected = None
                snapshot = fresh_snapshot
                risk_slots = fresh_risk_slots
                correlated_slots = fresh_correlated_slots
                portfolio_risk = fresh_portfolio_risk

        if selected is not None:
            execution_attempted = True
            try:
                if selected_is_stack:
                    coordinator = ProfitableStackCoordinator(
                        private=private,
                        writer=writer,
                        linkage=linkage,
                        stack_store=stack_store,
                        arm=arm,
                        safety=safety,
                    )
                    execution_result = coordinator.execute(
                        selected.readiness,
                        signal_id=selected.signal_id,
                        instrument=selected.instrument,
                    )
                else:
                    coordinator = DurableExecutionCoordinator(
                        private=private,
                        writer=writer,
                        linkage=linkage,
                        stack_store=stack_store,
                        arm=arm,
                        safety=safety,
                    )
                    execution_result = coordinator.execute(
                        selected.readiness,
                        signal_id=selected.signal_id,
                        instrument=selected.instrument,
                        risk_slots_in_use=risk_slots,
                        correlated_risk_slots_in_use=correlated_slots,
                        portfolio_planned_risk=portfolio_risk,
                    )
                orders_submitted = 1
            except UnknownSubmissionOutcome as exc:
                execution_error = f"UNKNOWN_SUBMISSION_OUTCOME:{exc.client_id}:{exc}"
            except (
                BinanceOrderSubmissionError,
                DurableExecutionError,
                StackExecutionError,
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
        stack_recovery=_recovery_dict(recovery_result),
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
