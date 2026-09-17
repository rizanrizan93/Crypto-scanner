from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal

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
from crypto_scanner.binance.public_rest import (
    BinanceDemoPublicRestClient,
    BinancePublicApiError,
)
from crypto_scanner.config import load_runtime_config
from crypto_scanner.discovery_pipeline import DiscoveryPipeline, MicrostructureSnapshot
from crypto_scanner.durable_execution import DurableExecutionCoordinator, DurableExecutionError
from crypto_scanner.execution_plan import (
    ExecutionPlanError,
    TestnetExecutionArm,
    build_entry_order_plan,
)
from crypto_scanner.fast_lane import FastLaneEvidence, evaluate_execution_readiness
from crypto_scanner.funding_oi_demo import (
    FUNDING_OI_STRATEGY_ID,
    FundingOiLeg,
    build_funding_oi_decision,
    filter_funding_oi_candidates,
)
from crypto_scanner.funding_oi_state import roll_daily_oi_snapshot
from crypto_scanner.hot_watch import select_hot_candidates
from crypto_scanner.lifecycle import recover_authoritative_state
from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig
from crypto_scanner.safety import SafetyContract
from crypto_scanner.scanner_cycle import (
    candidate_account_skip_reason,
    evaluate_account_execution_gate,
)
from crypto_scanner.stack_recovery import recover_stack_transactions
from crypto_scanner.stack_store import DurableStackStore
from crypto_scanner.strategy_params import load_strategy_parameters
from crypto_scanner.trade_linkage import DurableTradeLinkage

PARALLEL_FORWARD_DEMO_STAGE = "FORWARD_DEMO_PARALLEL"


class FundingOiCycleError(RuntimeError):
    """Raised when the Funding+OI parallel Demo lane cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class FundingOiCycleResult:
    status: str
    strategy_id: str
    promotion_stage: str
    signal_day: str | None
    oi_snapshot_day: str
    oi_previous_day: str | None
    active_legs: tuple[str, ...]
    run_id: str | None
    eligible_candidates: tuple[str, ...]
    signal_id: str | None
    executed_symbol: str | None
    risk_fraction: str | None
    account_blockers: tuple[str, ...]
    execution_error: str | None
    live_trading_locked: bool
    readiness_rejections: tuple[dict[str, object], ...] = ()


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _leg_label(leg: FundingOiLeg) -> str:
    return (
        f"{leg.symbol}:{leg.direction.value}:z={leg.funding_z}:"
        f"oi={leg.oi_growth}:w={leg.target_weight}"
    )


def _scope_discovery_to_active_symbols(results, active_symbols: frozenset[str]):
    """Prevent unrelated global candidates from starving active Funding/OI legs."""
    if not active_symbols:
        return ()
    return tuple(result for result in results if result.symbol.upper() in active_symbols)


def _collect_oi_values(
    public: BinanceDemoPublicRestClient,
    universe: tuple[str, ...],
) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for raw_symbol in universe:
        symbol = raw_symbol.upper()
        try:
            ticker = public.get_ticker(symbol)
        except (BinancePublicApiError, ValueError, RuntimeError):
            continue
        if ticker.open_interest is None or ticker.open_interest <= 0:
            continue
        if ticker.mark_price <= 0:
            continue
        values[symbol] = ticker.open_interest * ticker.mark_price
    return values


def run_funding_oi_cycle() -> FundingOiCycleResult:
    safety = SafetyContract()
    safety.validate()
    if not safety.live_trading_locked:
        raise FundingOiCycleError("LIVE lock must remain enabled")

    arm = TestnetExecutionArm.from_environment()
    arm.require_enabled()
    runtime = load_runtime_config()
    persistence = SupabasePersistenceConfig.from_environment()
    if not persistence.enabled:
        raise FundingOiCycleError("dedicated Crypto Scanner Supabase is required")
    strategy_params = load_strategy_parameters(persistence)

    credentials = BinanceDemoCredentials.from_environment()
    run_id: str | None = None
    signal_id: str | None = None
    executed_symbol: str | None = None
    executed_risk: Decimal | None = None
    execution_error: str | None = None
    eligible_symbols: tuple[str, ...] = ()
    account_blockers: tuple[str, ...] = ()
    readiness_rejections: list[dict[str, object]] = []

    with (
        BinanceDemoPublicRestClient(base_url=runtime.binance_rest_url) as public,
        BinanceDemoMicrostructureClient(base_url=runtime.binance_rest_url) as micro,
        BinanceDemoPrivateReadOnlyClient(
            credentials,
            base_url=runtime.binance_rest_url,
        ) as private,
        BinanceTestnetOrderClient(
            credentials,
            arm,
            base_url=runtime.binance_rest_url,
        ) as writer,
        DurableTradeLinkage(persistence) as linkage,
        DurableStackStore(persistence) as stack_store,
    ):
        now_ms = _now_ms()
        recovery = recover_stack_transactions(private, writer, stack_store, now_ms=now_ms)
        snapshot = recover_authoritative_state(private)
        gate = evaluate_account_execution_gate(snapshot, safety)
        blockers = list(gate.reasons)
        blockers.extend(recovery.blockers)
        account_blockers = tuple(dict.fromkeys(blockers))

        oi_values = _collect_oi_values(public, runtime.universe)
        if not oi_values:
            raise FundingOiCycleError("no usable OI values were available from Binance Demo")
        current_day = datetime.fromtimestamp(now_ms / 1000, tz=UTC).date()
        oi_state = roll_daily_oi_snapshot(
            persistence,
            current_day=current_day,
            values=oi_values,
            updated_at_ms=now_ms,
        )
        decision = build_funding_oi_decision(
            public,
            runtime.universe,
            oi_state.growth(),
            now_ms=now_ms,
        )
        leg_map = {(leg.symbol, leg.direction): leg for leg in decision.legs}

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
        active_symbols = frozenset(leg.symbol.upper() for leg in decision.legs)
        strategy_results = _scope_discovery_to_active_symbols(discovery.results, active_symbols)
        hot = select_hot_candidates(strategy_results)
        eligible = filter_funding_oi_candidates(hot, decision)
        eligible_symbols = tuple(candidate.symbol for candidate in eligible)

        if account_blockers:
            return FundingOiCycleResult(
                status="PASS_FORWARD_DEMO_PARALLEL_BLOCKED_BY_ACCOUNT_GATE",
                strategy_id=FUNDING_OI_STRATEGY_ID,
                promotion_stage=PARALLEL_FORWARD_DEMO_STAGE,
                signal_day=decision.signal_day,
                oi_snapshot_day=oi_state.current_day.isoformat(),
                oi_previous_day=(
                    oi_state.previous_day.isoformat() if oi_state.previous_day else None
                ),
                active_legs=tuple(_leg_label(leg) for leg in decision.legs),
                run_id=run_id,
                eligible_candidates=eligible_symbols,
                signal_id=None,
                executed_symbol=None,
                risk_fraction=None,
                account_blockers=account_blockers,
                execution_error=None,
                live_trading_locked=True,
            )

        equity = snapshot.wallet.total_equity
        if equity is None or equity <= 0:
            raise FundingOiCycleError("authoritative equity missing")
        risk_slots, correlated_slots, portfolio_risk = stack_store.risk_accounting(
            snapshot.positions,
            equity=equity,
            safety=safety,
        )

        for candidate in eligible:
            leg = leg_map[(candidate.symbol, candidate.direction)]
            if leg.risk_fraction <= 0:
                continue
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
                # Shared Binance net exposure cannot cleanly attribute two strategy lanes.
                continue

            try:
                instrument = public.get_instrument(candidate.symbol)
                candles_3m = public.get_klines(candidate.symbol, "3", limit=200)
                candles_5m = public.get_klines(candidate.symbol, "5", limit=200)
                readiness: ReadinessDecision | None = None
                geometry_now_ms = _now_ms()

                for micro_round in range(1, DEMO_TEMPORAL_CONFIRMATION_ROUNDS + 1):
                    if micro_round > 1:
                        time.sleep(DEMO_TEMPORAL_CONFIRMATION_INTERVAL_SECONDS)
                    ticker = public.get_ticker(candidate.symbol)
                    quote_timestamp_ms = _now_ms()
                    fresh = micro.get_evidence(candidate.symbol)
                    geometry_now_ms = _now_ms()
                    readiness = evaluate_execution_readiness(
                        candidate,
                        candles_3m=candles_3m,
                        candles_5m=candles_5m,
                        ticker=ticker,
                        instrument=instrument,
                        evidence=FastLaneEvidence(
                            quote_timestamp_ms=quote_timestamp_ms,
                            candidate_timestamp_ms=discovery.completed_at_ms,
                            orderbook_timestamp_ms=min(fresh.observed_at_ms, geometry_now_ms),
                            orderbook_imbalance=fresh.orderbook_imbalance,
                            taker_pressure=fresh.taker_pressure,
                            exchange_healthy=True,
                            orderbook_healthy=True,
                        ),
                        now_ms=geometry_now_ms,
                        strategy=strategy_params,
                    )
                    if readiness.execution_ready:
                        break
                    readiness_rejections.append(
                        {
                            "symbol": candidate.symbol,
                            "direction": candidate.direction.value,
                            "micro_round": micro_round,
                            "promoted_watch": DEMO_ACQUISITION_REASON in candidate.reasons,
                            "reasons": list(readiness.reasons),
                            "orderbook_imbalance": str(fresh.orderbook_imbalance),
                            "taker_pressure": str(fresh.taker_pressure),
                        }
                    )
                    if not should_retry_demo_temporal_confirmation(
                        candidate,
                        readiness,
                        round_index=micro_round,
                    ):
                        break

                if readiness is None or not readiness.execution_ready:
                    continue

                signal_id = linkage.save_execution_ready_signal(
                    run_id=run_id,
                    candidate=candidate,
                    readiness=readiness,
                    candidate_timestamp_ms=discovery.completed_at_ms,
                    geometry_created_at_ms=geometry_now_ms,
                    strategy_id=FUNDING_OI_STRATEGY_ID,
                    promotion_stage=PARALLEL_FORWARD_DEMO_STAGE,
                    strategy_params=strategy_params.to_dict(),
                )

                build_entry_order_plan(
                    readiness,
                    signal_id=signal_id,
                    wallet=snapshot.wallet,
                    positions=snapshot.positions,
                    instrument=instrument,
                    safety=safety,
                    risk_fraction=leg.risk_fraction,
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
                    risk_fraction=leg.risk_fraction,
                    risk_slots_in_use=risk_slots,
                    correlated_risk_slots_in_use=correlated_slots,
                    portfolio_planned_risk=portfolio_risk,
                )
                executed_symbol = result.symbol
                executed_risk = leg.risk_fraction
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
        status = "FAIL_FORWARD_DEMO_PARALLEL_EXECUTION"
    elif executed_symbol is not None:
        status = "PASS_FORWARD_DEMO_PARALLEL_EXECUTED"
    else:
        status = "PASS_FORWARD_DEMO_PARALLEL_NO_ENTRY"

    return FundingOiCycleResult(
        status=status,
        strategy_id=FUNDING_OI_STRATEGY_ID,
        promotion_stage=PARALLEL_FORWARD_DEMO_STAGE,
        signal_day=decision.signal_day,
        oi_snapshot_day=oi_state.current_day.isoformat(),
        oi_previous_day=(oi_state.previous_day.isoformat() if oi_state.previous_day else None),
        active_legs=tuple(_leg_label(leg) for leg in decision.legs),
        run_id=run_id,
        eligible_candidates=eligible_symbols,
        signal_id=signal_id,
        executed_symbol=executed_symbol,
        risk_fraction=str(executed_risk) if executed_risk is not None else None,
        account_blockers=account_blockers,
        execution_error=execution_error,
        live_trading_locked=True,
        readiness_rejections=tuple(readiness_rejections),
    )


def main() -> None:
    result = run_funding_oi_cycle()
    print(json.dumps(asdict(result), indent=2, sort_keys=True, default=str))
    if result.execution_error is not None:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
