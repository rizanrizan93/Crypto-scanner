from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.microstructure import (
    BinanceDemoMicrostructureClient,
    BinanceMicrostructureError,
)
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient
from crypto_scanner.binance.private_write import (
    BinanceOrderSubmissionError,
    BinanceTestnetOrderClient,
    UnknownSubmissionOutcome,
)
from crypto_scanner.binance.public_rest import BinanceDemoPublicRestClient
from crypto_scanner.config import load_runtime_config
from crypto_scanner.discovery_pipeline import DiscoveryPipeline, MicrostructureSnapshot
from crypto_scanner.durable_execution import DurableExecutionCoordinator, DurableExecutionError
from crypto_scanner.execution_plan import (
    ExecutionPlanError,
    TestnetExecutionArm,
    build_entry_order_plan,
)
from crypto_scanner.fast_lane import FastLaneEvidence, evaluate_execution_readiness
from crypto_scanner.hot_watch import select_hot_candidates
from crypto_scanner.lifecycle import recover_authoritative_state
from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig
from crypto_scanner.regime_specialist_demo import (
    REGIME_SPECIALIST_STRATEGY_ID,
    build_regime_specialist_decision,
    filter_regime_specialist_candidates,
)
from crypto_scanner.safety import SafetyContract
from crypto_scanner.scanner_cycle import (
    candidate_account_skip_reason,
    evaluate_account_execution_gate,
)
from crypto_scanner.stack_recovery import recover_stack_transactions
from crypto_scanner.stack_store import DurableStackStore
from crypto_scanner.strategy_promotion import PromotionStage, load_strategy_runtime
from crypto_scanner.trade_linkage import DurableTradeLinkage


class RegimeSpecialistCycleError(RuntimeError):
    """Raised when the dedicated FORWARD_DEMO regime cycle cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class RegimeSpecialistCycleResult:
    status: str
    strategy_id: str
    promotion_stage: str
    global_regime: str
    bull_switch_active: bool
    bear_short_symbols: tuple[str, ...]
    risk_fraction: str
    run_id: str | None
    eligible_candidates: tuple[str, ...]
    signal_id: str | None
    executed_symbol: str | None
    account_blockers: tuple[str, ...]
    execution_error: str | None
    live_trading_locked: bool


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def run_regime_specialist_cycle() -> RegimeSpecialistCycleResult:
    safety = SafetyContract()
    safety.validate()
    if not safety.live_trading_locked:
        raise RegimeSpecialistCycleError("LIVE lock must remain enabled")

    arm = TestnetExecutionArm.from_environment()
    arm.require_enabled()
    runtime = load_runtime_config()
    persistence = SupabasePersistenceConfig.from_environment()
    if not persistence.enabled:
        raise RegimeSpecialistCycleError("dedicated Crypto Scanner Supabase is required")

    strategy_runtime = load_strategy_runtime(persistence)
    if (
        strategy_runtime.strategy_id != REGIME_SPECIALIST_STRATEGY_ID
        or strategy_runtime.promotion_stage != PromotionStage.FORWARD_DEMO.value
        or not strategy_runtime.execution_authorized
        or not strategy_runtime.forward_demo_candidate
    ):
        raise RegimeSpecialistCycleError(
            "regime specialist must be the authoritative FORWARD_DEMO candidate before execution"
        )

    credentials = BinanceDemoCredentials.from_environment()
    run_id: str | None = None
    signal_id: str | None = None
    executed_symbol: str | None = None
    execution_error: str | None = None
    eligible_symbols: tuple[str, ...] = ()
    account_blockers: tuple[str, ...] = ()

    with (
        BinanceDemoPublicRestClient(base_url=runtime.binance_rest_url) as public,
        BinanceDemoMicrostructureClient(base_url=runtime.binance_rest_url) as micro,
        BinanceDemoPrivateReadOnlyClient(credentials, base_url=runtime.binance_rest_url) as private,
        BinanceTestnetOrderClient(credentials, arm, base_url=runtime.binance_rest_url) as writer,
        DurableTradeLinkage(persistence) as linkage,
        DurableStackStore(persistence) as stack_store,
    ):
        recovery = recover_stack_transactions(private, writer, stack_store, now_ms=_now_ms())
        snapshot = recover_authoritative_state(private)
        gate = evaluate_account_execution_gate(snapshot, safety)
        blockers = list(gate.reasons)
        blockers.extend(recovery.blockers)
        account_blockers = tuple(dict.fromkeys(blockers))

        decision = build_regime_specialist_decision(
            public,
            runtime.universe,
            now_ms=_now_ms(),
        )

        discovery_micro: dict[str, MicrostructureSnapshot] = {}
        for symbol in runtime.universe:
            try:
                evidence = micro.get_evidence(symbol)
            except (BinanceMicrostructureError, ValueError, RuntimeError):
                continue
            discovery_micro[symbol] = MicrostructureSnapshot(
                orderbook_imbalance=evidence.orderbook_imbalance,
                taker_pressure=evidence.taker_pressure,
            )

        discovery = DiscoveryPipeline(public, universe=runtime.universe).run(discovery_micro)
        run_id = linkage.save_discovery_run(discovery, execution_armed=True)
        hot = select_hot_candidates(discovery.results)
        eligible = filter_regime_specialist_candidates(hot, decision)
        eligible_symbols = tuple(candidate.symbol for candidate in eligible)

        if account_blockers:
            return RegimeSpecialistCycleResult(
                status="PASS_FORWARD_DEMO_BLOCKED_BY_ACCOUNT_GATE",
                strategy_id=REGIME_SPECIALIST_STRATEGY_ID,
                promotion_stage=PromotionStage.FORWARD_DEMO.value,
                global_regime=decision.regime.value,
                bull_switch_active=decision.bull_switch_active,
                bear_short_symbols=decision.bear_short_symbols,
                risk_fraction=str(decision.risk_fraction),
                run_id=run_id,
                eligible_candidates=eligible_symbols,
                signal_id=None,
                executed_symbol=None,
                account_blockers=account_blockers,
                execution_error=None,
                live_trading_locked=True,
            )

        equity = snapshot.wallet.total_equity
        if equity is None or equity <= 0:
            raise RegimeSpecialistCycleError("authoritative equity missing")
        risk_slots, correlated_slots, portfolio_risk = stack_store.risk_accounting(
            snapshot.positions,
            equity=equity,
            safety=safety,
        )

        for candidate in eligible:
            skip = candidate_account_skip_reason(
                candidate.symbol,
                snapshot,
                safety,
                risk_slots_in_use=risk_slots,
                correlated_risk_slots_in_use=correlated_slots,
            )
            if skip is not None:
                continue
            if any(position.symbol == candidate.symbol for position in snapshot.open_positions):
                # Forward-demo challenger never stacks; this keeps trade attribution clean.
                continue

            try:
                instrument = public.get_instrument(candidate.symbol)
                candles_3m = public.get_klines(candidate.symbol, "3", limit=200)
                candles_5m = public.get_klines(candidate.symbol, "5", limit=200)
                ticker = public.get_ticker(candidate.symbol)
                quote_timestamp_ms = _now_ms()
                fresh = micro.get_evidence(candidate.symbol)
                now_ms = _now_ms()
                readiness = evaluate_execution_readiness(
                    candidate,
                    candles_3m=candles_3m,
                    candles_5m=candles_5m,
                    ticker=ticker,
                    instrument=instrument,
                    evidence=FastLaneEvidence(
                        quote_timestamp_ms=quote_timestamp_ms,
                        candidate_timestamp_ms=discovery.completed_at_ms,
                        orderbook_timestamp_ms=min(fresh.observed_at_ms, now_ms),
                        orderbook_imbalance=fresh.orderbook_imbalance,
                        taker_pressure=fresh.taker_pressure,
                        exchange_healthy=True,
                        orderbook_healthy=True,
                    ),
                    now_ms=now_ms,
                    strategy=strategy_runtime.params,
                )
                if not readiness.execution_ready:
                    continue

                signal_id = linkage.save_execution_ready_signal(
                    run_id=run_id,
                    candidate=candidate,
                    readiness=readiness,
                    candidate_timestamp_ms=discovery.completed_at_ms,
                    geometry_created_at_ms=now_ms,
                    strategy_id=REGIME_SPECIALIST_STRATEGY_ID,
                    promotion_stage=PromotionStage.FORWARD_DEMO.value,
                    strategy_params=strategy_runtime.params.to_dict(),
                )

                build_entry_order_plan(
                    readiness,
                    signal_id=signal_id,
                    wallet=snapshot.wallet,
                    positions=snapshot.positions,
                    instrument=instrument,
                    safety=safety,
                    risk_fraction=decision.risk_fraction,
                    risk_slots_in_use=risk_slots,
                    correlated_risk_slots_in_use=correlated_slots,
                    portfolio_planned_risk=portfolio_risk,
                )

                coordinator = DurableExecutionCoordinator(
                    private=private,
                    writer=writer,
                    linkage=linkage,
                    stack_store=stack_store,
                    arm=arm,
                    safety=safety,
                )
                result = coordinator.execute(
                    readiness,
                    signal_id=signal_id,
                    instrument=instrument,
                    risk_fraction=decision.risk_fraction,
                    risk_slots_in_use=risk_slots,
                    correlated_risk_slots_in_use=correlated_slots,
                    portfolio_planned_risk=portfolio_risk,
                )
                executed_symbol = result.symbol
                break
            except UnknownSubmissionOutcome as exc:
                execution_error = f"UNKNOWN_SUBMISSION_OUTCOME:{exc.client_id}:{exc}"
                break
            except (
                BinanceOrderSubmissionError,
                BinanceMicrostructureError,
                DurableExecutionError,
                ExecutionPlanError,
                PersistenceError,
                ValueError,
                RuntimeError,
            ) as exc:
                execution_error = f"{type(exc).__name__}:{exc}"
                break

    if execution_error is not None:
        status = "FAIL_FORWARD_DEMO_EXECUTION"
    elif executed_symbol is not None:
        status = "PASS_FORWARD_DEMO_EXECUTED"
    else:
        status = "PASS_FORWARD_DEMO_NO_ENTRY"

    return RegimeSpecialistCycleResult(
        status=status,
        strategy_id=REGIME_SPECIALIST_STRATEGY_ID,
        promotion_stage=PromotionStage.FORWARD_DEMO.value,
        global_regime=decision.regime.value,
        bull_switch_active=decision.bull_switch_active,
        bear_short_symbols=decision.bear_short_symbols,
        risk_fraction=str(decision.risk_fraction),
        run_id=run_id,
        eligible_candidates=eligible_symbols,
        signal_id=signal_id,
        executed_symbol=executed_symbol,
        account_blockers=account_blockers,
        execution_error=execution_error,
        live_trading_locked=True,
    )


def main() -> None:
    result = run_regime_specialist_cycle()
    print(json.dumps(asdict(result), indent=2, sort_keys=True, default=str))
    if result.execution_error is not None:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
