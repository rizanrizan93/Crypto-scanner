from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

from crypto_scanner.binance.models import InstrumentInfo, OrderSnapshot, PositionSnapshot
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient, UserTradeFill
from crypto_scanner.binance.private_write import (
    BinanceOrderSubmissionError,
    BinanceTestnetOrderClient,
    UnknownSubmissionOutcome,
    deterministic_management_id,
)
from crypto_scanner.discovery import TradeDirection
from crypto_scanner.durable_execution import (
    DurableExecutionError,
    DurableExecutionResult,
    _average_fill_price,
    _required_leverage,
)
from crypto_scanner.execution_plan import (
    EntryOrderPlan,
    ExecutionPlanError,
    TestnetExecutionArm,
    build_entry_order_plan,
)
from crypto_scanner.fast_lane import ReadinessDecision, ReadinessStatus
from crypto_scanner.position_manager import ProtectionStatus, audit_symbol_protection
from crypto_scanner.position_manager_write import (
    PositionManagerError,
    replace_aggregate_protection,
)
from crypto_scanner.safety import SafetyContract
from crypto_scanner.stack_store import (
    DurableStackState,
    DurableStackStore,
    StackTransaction,
)
from crypto_scanner.stacking import (
    DurableLayer,
    StackClassification,
    StackTransactionState,
    build_aggregate_protection_geometry,
    direction_for_position,
    evaluate_stack_admission,
)
from crypto_scanner.trade_linkage import DurableTradeLinkage


class StackExecutionError(DurableExecutionError):
    """Raised when a profitable stacking transaction cannot finish safely."""


def _layer_payload(layer: DurableLayer) -> dict[str, object]:
    return {
        "signal_id": layer.signal_id,
        "classification": layer.classification.value,
        "direction": layer.direction.value,
        "qty": str(layer.qty),
        "entry_price": str(layer.entry_price),
        "stop_loss": str(layer.stop_loss),
        "tp1": str(layer.tp1),
        "tp2": str(layer.tp2),
        "risk_amount": str(layer.risk_amount),
        "opened_at_ms": layer.opened_at_ms,
        "client_order_id": layer.client_order_id,
    }


def _transaction_detail(
    *,
    pre_position_size: Decimal,
    pending_layer: DurableLayer,
    aggregate_stop: Decimal | None = None,
    aggregate_tp2: Decimal | None = None,
) -> str:
    return json.dumps(
        {
            "pre_position_size": str(pre_position_size),
            "pending_layer": _layer_payload(pending_layer),
            "aggregate_stop": str(aggregate_stop) if aggregate_stop is not None else None,
            "aggregate_tp2": str(aggregate_tp2) if aggregate_tp2 is not None else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class ProfitableStackCoordinator:
    """Add one same-side profitable layer to a Binance One-way net position.

    The transaction is fail-closed and durable. Existing SL/TP2 remain active while the
    new layer fills; replacement then installs/reconciles the new full-size STOP and TP2
    before stale scanner-owned legs are cancelled.
    """

    def __init__(
        self,
        *,
        private: BinanceDemoPrivateReadOnlyClient,
        writer: BinanceTestnetOrderClient,
        linkage: DurableTradeLinkage,
        stack_store: DurableStackStore,
        arm: TestnetExecutionArm,
        safety: SafetyContract | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.private = private
        self.writer = writer
        self.linkage = linkage
        self.stack_store = stack_store
        self.arm = arm
        self.safety = safety or SafetyContract()
        self.sleep = sleep
        self.now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)

    def _save_transaction(
        self,
        state: DurableStackState,
        *,
        signal_id: str,
        tx_state: StackTransactionState,
        started_at_ms: int,
        detail: str,
        old_stop_id: str | None,
        old_tp2_id: str | None,
        new_stop_id: str | None = None,
        new_tp2_id: str | None = None,
        quarantine_reason: str | None = None,
    ) -> DurableStackState:
        now = self.now_ms()
        updated = replace(
            state,
            transaction=StackTransaction(
                signal_id=signal_id,
                state=tx_state,
                started_at_ms=started_at_ms,
                updated_at_ms=now,
                old_stop_client_algo_id=old_stop_id,
                old_tp2_client_algo_id=old_tp2_id,
                new_stop_client_algo_id=new_stop_id,
                new_tp2_client_algo_id=new_tp2_id,
                detail=detail,
            ),
            quarantined=quarantine_reason is not None,
            quarantine_reason=quarantine_reason,
        )
        self.stack_store.save(updated, updated_at_ms=now)
        return updated

    def _reconcile_entry(self, plan: EntryOrderPlan, *, attempts: int = 12) -> OrderSnapshot:
        last: OrderSnapshot | None = None
        for _ in range(attempts):
            last = self.private.get_order_by_client_id(plan.symbol, plan.order_link_id)
            if last.order_status == "FILLED":
                return last
            if last.order_status in {"CANCELED", "EXPIRED", "REJECTED"}:
                break
            self.sleep(0.5)
        if last is not None and (last.cum_exec_qty or Decimal(0)) > 0:
            return last
        raise StackExecutionError(
            f"stack entry produced no confirmed fill; status={last.order_status if last else 'UNKNOWN'}"
        )

    def _entry_fills(self, order: OrderSnapshot) -> tuple[UserTradeFill, ...]:
        start_ms = max(0, (order.created_time_ms or self.now_ms()) - 60_000)
        fills = tuple(
            fill
            for fill in self.private.get_user_trades(
                order.symbol,
                start_time_ms=start_ms,
                limit=1000,
            )
            if fill.order_id == order.order_id
        )
        if not fills:
            raise StackExecutionError("stack fill could not be reconciled to user trades")
        return fills

    def _single_position(self, symbol: str) -> PositionSnapshot:
        matches = tuple(
            position
            for position in self.private.get_positions()
            if position.is_open and position.symbol == symbol
        )
        if len(matches) != 1:
            raise StackExecutionError(
                "stacking requires exactly one authoritative One-way net position"
            )
        return matches[0]

    def _rollback_layer(
        self,
        *,
        plan: EntryOrderPlan,
        filled_qty: Decimal,
        pre_position_size: Decimal,
    ) -> None:
        exit_side = "SELL" if plan.side == "Buy" else "BUY"
        client_id = deterministic_management_id(plan.symbol, plan.signal_id, "rollback")
        try:
            self.writer.submit_reduce_only_market_exit(
                symbol=plan.symbol,
                exit_side=exit_side,
                qty=filled_qty,
                client_order_id=client_id,
            )
        except UnknownSubmissionOutcome:
            raise
        rollback = self.private.get_order_by_client_id(plan.symbol, client_id)
        if rollback.order_status != "FILLED":
            raise StackExecutionError(
                f"stack rollback did not fill deterministically: {rollback.order_status}"
            )
        position = self._single_position(plan.symbol)
        if position.size != pre_position_size:
            raise StackExecutionError(
                "stack rollback did not restore pre-stack aggregate quantity"
            )

    def execute(
        self,
        readiness: ReadinessDecision,
        *,
        signal_id: str,
        instrument: InstrumentInfo,
        risk_fraction: Decimal = Decimal("0.005"),
    ) -> DurableExecutionResult:
        self.safety.validate()
        self.arm.require_enabled()
        if not self.safety.profitable_stacking_enabled:
            raise StackExecutionError("profitable stacking is disabled")
        if readiness.status is not ReadinessStatus.EXECUTION_READY or readiness.geometry is None:
            raise StackExecutionError("stacking requires fresh EXECUTION_READY geometry")
        if readiness.geometry.symbol != instrument.symbol:
            raise StackExecutionError("stack instrument does not match signal geometry")
        if self.private.get_position_mode_is_hedged():
            raise StackExecutionError("stacking requires Binance One-way Mode")

        symbol = readiness.geometry.symbol
        wallet = self.private.get_wallet_balance()
        if wallet.total_equity is None or wallet.total_equity <= 0:
            raise StackExecutionError("authoritative equity is missing or invalid")
        positions = self.private.get_positions()
        current = tuple(p for p in positions if p.is_open and p.symbol == symbol)
        if len(current) != 1:
            raise StackExecutionError("stacking requires an existing same-symbol position")
        position = current[0]
        if position.avg_price is None or position.mark_price is None:
            raise StackExecutionError("existing position is missing average/mark price")

        all_algos = self.private.get_open_algo_orders()
        protection = audit_symbol_protection(symbol, positions, all_algos)
        if (
            protection.status is not ProtectionStatus.PROTECTED
            or protection.stop is None
            or protection.take_profit is None
            or protection.stop.trigger_price is None
            or protection.take_profit.trigger_price is None
            or not protection.stop.client_algo_id.startswith("cs-")
            or not protection.take_profit.client_algo_id.startswith("cs-")
        ):
            raise StackExecutionError(
                "existing position must have scanner-owned exact full-size SL and TP2"
            )

        state = self.stack_store.load(symbol)
        if state is None or not state.layers or state.position_id is None:
            raise StackExecutionError("existing position lacks durable layer ledger")
        signal_record = self.stack_store.signal_runtime_record(signal_id)
        if not signal_record.is_fresh_unused(self.now_ms()):
            raise StackExecutionError("stack signal is stale, reused, or not EXECUTION_READY")

        total_slots, correlated_slots, portfolio_risk = self.stack_store.risk_accounting(
            positions,
            equity=wallet.total_equity,
            safety=self.safety,
        )
        admission = evaluate_stack_admission(
            position=position,
            protection=protection,
            readiness=readiness,
            signal_id=signal_id,
            signal_expires_at_ms=signal_record.expires_at_ms or 0,
            now_ms=self.now_ms(),
            ledger=state.ledger,
            total_risk_slots_in_use=total_slots,
            correlated_risk_slots_in_use=correlated_slots,
            portfolio_planned_risk=portfolio_risk,
            equity=wallet.total_equity,
            tick_size=instrument.tick_size,
            safety=self.safety,
        )
        if not admission.allowed or admission.classification is None:
            raise StackExecutionError(
                "stack admission rejected: " + ",".join(admission.reasons)
            )

        try:
            plan = build_entry_order_plan(
                readiness,
                signal_id=signal_id,
                wallet=wallet,
                positions=positions,
                instrument=instrument,
                safety=self.safety,
                risk_fraction=risk_fraction,
                allow_same_symbol=True,
                risk_slots_in_use=total_slots,
                correlated_risk_slots_in_use=correlated_slots,
                portfolio_planned_risk=portfolio_risk,
            )
        except ExecutionPlanError as exc:
            raise StackExecutionError(str(exc)) from exc

        # Preflight the expected post-fill geometry before any write. This avoids
        # knowingly adding a layer that could not keep the aggregate winner protected.
        expected_qty = position.size + plan.qty
        expected_avg = (
            position.avg_price * position.size + plan.entry_price * plan.qty
        ) / expected_qty
        build_aggregate_protection_geometry(
            direction=direction_for_position(position),
            aggregate_qty=expected_qty,
            aggregate_entry_price=expected_avg,
            mark_price=position.mark_price,
            old_stop=protection.stop.trigger_price,
            old_tp2=protection.take_profit.trigger_price,
            new_signal_stop=plan.stop_loss,
            new_signal_tp2=plan.take_profit_2,
            tick_size=instrument.tick_size,
            layers=state.layers,
            new_layer_qty=plan.qty,
            new_layer_entry_price=plan.entry_price,
        )

        available = wallet.total_available_balance
        if available is None or available <= 0:
            raise StackExecutionError("authoritative available balance is invalid")
        layer_leverage = _required_leverage(plan, self.safety, available_balance=available)
        current_leverage = position.leverage
        if current_leverage is None or current_leverage <= 0:
            raise StackExecutionError("existing position leverage is invalid")
        if current_leverage != current_leverage.to_integral_value():
            raise StackExecutionError("existing Binance leverage must be an integer")
        leverage = max(layer_leverage, int(current_leverage))
        if leverage > self.safety.max_leverage:
            raise StackExecutionError("aggregate symbol leverage would breach safety cap")

        started_at = self.now_ms()
        pending = DurableLayer(
            signal_id=signal_id,
            classification=admission.classification,
            direction=readiness.geometry.direction,
            qty=plan.qty,
            entry_price=plan.entry_price,
            stop_loss=plan.stop_loss,
            tp1=plan.take_profit_1,
            tp2=plan.take_profit_2,
            risk_amount=plan.risk_amount,
            opened_at_ms=started_at,
            client_order_id=plan.order_link_id,
        )
        detail = _transaction_detail(
            pre_position_size=position.size,
            pending_layer=pending,
        )
        state = self._save_transaction(
            state,
            signal_id=signal_id,
            tx_state=StackTransactionState.PLANNED,
            started_at_ms=started_at,
            detail=detail,
            old_stop_id=protection.stop.client_algo_id,
            old_tp2_id=protection.take_profit.client_algo_id,
        )
        self.linkage.save_entry_plan(
            plan,
            status="STACK_PLANNED",
            created_at_ms=started_at,
            updated_at_ms=started_at,
        )
        self.writer.set_leverage(symbol, leverage)

        try:
            ack = self.writer.submit_entry(plan)
        except UnknownSubmissionOutcome:
            self.linkage.save_entry_plan(
                plan,
                status="STACK_UNKNOWN_OUTCOME",
                updated_at_ms=self.now_ms(),
            )
            self._save_transaction(
                state,
                signal_id=signal_id,
                tx_state=StackTransactionState.QUARANTINED,
                started_at_ms=started_at,
                detail=detail,
                old_stop_id=protection.stop.client_algo_id,
                old_tp2_id=protection.take_profit.client_algo_id,
                quarantine_reason="UNKNOWN_STACK_ENTRY_OUTCOME",
            )
            raise
        except BinanceOrderSubmissionError:
            self.linkage.save_entry_plan(
                plan,
                status="STACK_REJECTED",
                updated_at_ms=self.now_ms(),
            )
            self.stack_store.save(
                replace(state, transaction=None, quarantined=False, quarantine_reason=None),
                updated_at_ms=self.now_ms(),
            )
            raise

        state = self._save_transaction(
            state,
            signal_id=signal_id,
            tx_state=StackTransactionState.ENTRY_SUBMITTED,
            started_at_ms=started_at,
            detail=detail,
            old_stop_id=protection.stop.client_algo_id,
            old_tp2_id=protection.take_profit.client_algo_id,
        )
        self.linkage.save_entry_plan(
            plan,
            status="STACK_PENDING_RECONCILIATION",
            venue_order_id=ack.order_id,
            created_at_ms=ack.exchange_time_ms or started_at,
            updated_at_ms=ack.exchange_time_ms or self.now_ms(),
        )

        order = self._reconcile_entry(plan)
        filled_qty = order.cum_exec_qty or Decimal(0)
        if filled_qty <= 0 or filled_qty > plan.qty:
            raise StackExecutionError("reconciled stack quantity is invalid")
        fills = self._entry_fills(order)
        fill_qty = sum((fill.qty for fill in fills), Decimal(0))
        if fill_qty != filled_qty:
            raise StackExecutionError("stack user-trade fills do not match order fill quantity")
        for fill in fills:
            self.linkage.save_fill(fill, client_order_id=plan.order_link_id)
        layer_entry = order.avg_price or _average_fill_price(fills)
        opened_at = min(fill.time_ms for fill in fills)

        aggregate_position = self._single_position(symbol)
        if aggregate_position.side != position.side:
            raise StackExecutionError("stack fill changed net direction unexpectedly")
        if aggregate_position.size != position.size + filled_qty:
            raise StackExecutionError("aggregate quantity changed outside stack transaction")
        if aggregate_position.avg_price is None or aggregate_position.mark_price is None:
            raise StackExecutionError("post-stack aggregate position lacks average/mark price")

        pending = replace(
            pending,
            qty=filled_qty,
            entry_price=layer_entry,
            risk_amount=filled_qty * readiness.geometry.initial_risk,
            opened_at_ms=opened_at,
        )
        try:
            aggregate_geometry = build_aggregate_protection_geometry(
                direction=readiness.geometry.direction,
                aggregate_qty=aggregate_position.size,
                aggregate_entry_price=aggregate_position.avg_price,
                mark_price=aggregate_position.mark_price,
                old_stop=protection.stop.trigger_price,
                old_tp2=protection.take_profit.trigger_price,
                new_signal_stop=plan.stop_loss,
                new_signal_tp2=plan.take_profit_2,
                tick_size=instrument.tick_size,
                layers=state.layers,
                new_layer_qty=filled_qty,
                new_layer_entry_price=layer_entry,
            )
        except ValueError as exc:
            try:
                self._rollback_layer(
                    plan=plan,
                    filled_qty=filled_qty,
                    pre_position_size=position.size,
                )
            except Exception as rollback_exc:
                detail = _transaction_detail(
                    pre_position_size=position.size,
                    pending_layer=pending,
                )
                self._save_transaction(
                    state,
                    signal_id=signal_id,
                    tx_state=StackTransactionState.QUARANTINED,
                    started_at_ms=started_at,
                    detail=detail,
                    old_stop_id=protection.stop.client_algo_id,
                    old_tp2_id=protection.take_profit.client_algo_id,
                    quarantine_reason=f"POST_FILL_GEOMETRY_AND_ROLLBACK_UNCERTAIN:{rollback_exc}",
                )
                raise StackExecutionError("post-fill stack geometry unsafe; rollback uncertain") from rollback_exc
            self.linkage.save_entry_plan(
                plan,
                status="STACK_ROLLED_BACK_GEOMETRY",
                venue_order_id=order.order_id,
                avg_price=layer_entry,
                updated_at_ms=self.now_ms(),
            )
            self.stack_store.save(
                replace(state, transaction=None, quarantined=False, quarantine_reason=None),
                updated_at_ms=self.now_ms(),
            )
            raise StackExecutionError("post-fill stack geometry unsafe; layer rolled back") from exc

        max_portfolio_risk = wallet.total_equity * Decimal(
            str(self.safety.max_portfolio_risk_fraction)
        )
        if aggregate_geometry.aggregate_risk_amount > max_portfolio_risk:
            raise StackExecutionError("aggregate protected risk exceeds portfolio hard cap")

        detail = _transaction_detail(
            pre_position_size=position.size,
            pending_layer=pending,
            aggregate_stop=aggregate_geometry.stop_loss,
            aggregate_tp2=aggregate_geometry.take_profit_2,
        )
        state = self._save_transaction(
            state,
            signal_id=signal_id,
            tx_state=StackTransactionState.FILLED,
            started_at_ms=started_at,
            detail=detail,
            old_stop_id=protection.stop.client_algo_id,
            old_tp2_id=protection.take_profit.client_algo_id,
        )

        new_stop_id: str | None = None
        new_tp2_id: str | None = None

        def stage_callback(stage: str, client_id: str | None) -> None:
            nonlocal state, new_stop_id, new_tp2_id
            tx_state = StackTransactionState(stage)
            if tx_state is StackTransactionState.NEW_STOP_ACTIVE:
                new_stop_id = client_id
            elif tx_state is StackTransactionState.NEW_TP_ACTIVE:
                new_tp2_id = client_id
            state = self._save_transaction(
                state,
                signal_id=signal_id,
                tx_state=tx_state,
                started_at_ms=started_at,
                detail=detail,
                old_stop_id=protection.stop.client_algo_id,
                old_tp2_id=protection.take_profit.client_algo_id,
                new_stop_id=new_stop_id,
                new_tp2_id=new_tp2_id,
            )

        try:
            replace_aggregate_protection(
                self.private,
                self.writer,
                symbol=symbol,
                stop_trigger=aggregate_geometry.stop_loss,
                tp2_trigger=aggregate_geometry.take_profit_2,
                management_seed=signal_id,
                on_stage=stage_callback,
            )
        except (UnknownSubmissionOutcome, BinanceOrderSubmissionError, PositionManagerError) as exc:
            self._save_transaction(
                state,
                signal_id=signal_id,
                tx_state=StackTransactionState.QUARANTINED,
                started_at_ms=started_at,
                detail=detail,
                old_stop_id=protection.stop.client_algo_id,
                old_tp2_id=protection.take_profit.client_algo_id,
                new_stop_id=new_stop_id,
                new_tp2_id=new_tp2_id,
                quarantine_reason=f"STACK_PROTECTION_REPLACEMENT_UNCERTAIN:{type(exc).__name__}",
            )
            self.linkage.save_entry_plan(
                plan,
                status="STACK_FILLED_PROTECTION_QUARANTINED",
                venue_order_id=order.order_id,
                avg_price=layer_entry,
                updated_at_ms=self.now_ms(),
            )
            raise

        final_stop_id = deterministic_management_id(symbol, signal_id, "slr")
        final_tp2_id = deterministic_management_id(symbol, signal_id, "tp2r")
        final_state = DurableStackState(
            symbol=symbol,
            position_id=state.position_id,
            direction=readiness.geometry.direction,
            layers=state.layers + (pending,),
            aggregate_stop_loss=aggregate_geometry.stop_loss,
            aggregate_tp2=aggregate_geometry.take_profit_2,
            stop_client_algo_id=final_stop_id,
            tp2_client_algo_id=final_tp2_id,
            transaction=None,
            quarantined=False,
            quarantine_reason=None,
        )
        self.stack_store.save(final_state, updated_at_ms=self.now_ms())
        self.linkage.save_entry_plan(
            plan,
            status="STACK_FILLED_PROTECTED",
            venue_order_id=order.order_id,
            avg_price=layer_entry,
            created_at_ms=order.created_time_ms or opened_at,
            updated_at_ms=order.updated_time_ms or self.now_ms(),
        )

        return DurableExecutionResult(
            signal_id=signal_id,
            position_id=state.position_id or "",
            symbol=symbol,
            side=plan.side,
            client_order_id=plan.order_link_id,
            venue_order_id=order.order_id,
            filled_qty=filled_qty,
            average_entry_price=layer_entry,
            leverage=leverage,
            stop_client_algo_id=final_stop_id,
            tp1_client_algo_id=None,
            tp2_client_algo_id=final_tp2_id,
        )
