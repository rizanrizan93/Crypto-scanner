from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from crypto_scanner.binance.models import InstrumentInfo, OrderSnapshot, PositionSnapshot
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient, UserTradeFill
from crypto_scanner.binance.private_write import (
    AlgoSubmissionAck,
    BinanceOrderSubmissionError,
    BinanceTestnetOrderClient,
    SplitProtectionPlan,
    UnknownSubmissionOutcome,
    build_split_protection_plan,
)
from crypto_scanner.execution_plan import (
    EntryOrderPlan,
    ExecutionPlanError,
    TestnetExecutionArm,
    build_entry_order_plan,
)
from crypto_scanner.fast_lane import ReadinessDecision, ReadinessStatus
from crypto_scanner.persistence import PersistenceError
from crypto_scanner.safety import SafetyContract
from crypto_scanner.trade_linkage import DurableTradeLinkage


class DurableExecutionError(RuntimeError):
    """Raised when a durably-linked Demo execution cannot complete safely."""


@dataclass(frozen=True, slots=True)
class DurableExecutionResult:
    signal_id: str
    position_id: str
    symbol: str
    side: str
    client_order_id: str
    venue_order_id: str
    filled_qty: Decimal
    average_entry_price: Decimal
    leverage: int
    stop_client_algo_id: str
    tp1_client_algo_id: str | None
    tp2_client_algo_id: str


def _required_leverage(plan: EntryOrderPlan, safety: SafetyContract) -> int:
    value = plan.leverage_equivalent.to_integral_value(rounding=ROUND_CEILING)
    leverage = max(1, int(value))
    if leverage > safety.max_leverage:
        raise DurableExecutionError("planned leverage exceeds safety contract")
    return leverage


def _average_fill_price(fills: tuple[UserTradeFill, ...]) -> Decimal:
    qty = sum((fill.qty for fill in fills), Decimal(0))
    if qty <= 0:
        raise DurableExecutionError("confirmed entry has no durable fill quantity")
    quote = sum((fill.price * fill.qty for fill in fills), Decimal(0))
    return quote / qty


class DurableExecutionCoordinator:
    """One durable scanner signal -> one protected Binance Demo position.

    Submission is never retried here. Unknown write outcomes are persisted as such and
    must be reconciled by deterministic client id before a later recovery process acts.
    """

    def __init__(
        self,
        *,
        private: BinanceDemoPrivateReadOnlyClient,
        writer: BinanceTestnetOrderClient,
        linkage: DurableTradeLinkage,
        arm: TestnetExecutionArm,
        safety: SafetyContract | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.private = private
        self.writer = writer
        self.linkage = linkage
        self.arm = arm
        self.safety = safety or SafetyContract()
        self.sleep = sleep
        self.now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)

    def _reconcile_entry(
        self,
        plan: EntryOrderPlan,
        *,
        attempts: int = 12,
    ) -> OrderSnapshot:
        if not 1 <= attempts <= 30:
            raise ValueError("reconciliation attempts must be between 1 and 30")
        last: OrderSnapshot | None = None
        for _ in range(attempts):
            last = self.private.get_order_by_client_id(plan.symbol, plan.order_link_id)
            if last.order_status == "FILLED":
                return last
            if last.order_status in {"CANCELED", "EXPIRED", "REJECTED"}:
                break
            self.sleep(Decimal("0.5"))
        if last is None:
            raise DurableExecutionError("entry reconciliation returned no order state")
        if (last.cum_exec_qty or Decimal(0)) > 0:
            return last
        raise DurableExecutionError(
            f"entry produced no confirmed fill; status={last.order_status or 'UNKNOWN'}"
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
            raise DurableExecutionError("filled entry could not be reconciled to user-trade fills")
        return fills

    def _verify_algo_new(self, ack: AlgoSubmissionAck) -> None:
        state = self.private.get_algo_order_by_client_id(ack.client_algo_id)
        if state.status != "NEW" or not state.reduce_only:
            raise DurableExecutionError(
                f"conditional protector is not active NEW/reduceOnly: {ack.client_algo_id} "
                f"status={state.status} reduce_only={state.reduce_only}"
            )

    def _install_protection(
        self,
        plan: EntryOrderPlan,
        filled_qty: Decimal,
        instrument: InstrumentInfo,
    ) -> tuple[SplitProtectionPlan, AlgoSubmissionAck, AlgoSubmissionAck | None, AlgoSubmissionAck]:
        protection = build_split_protection_plan(plan, filled_qty, instrument)

        stop_ack = self.writer.submit_conditional_exit(protection.stop_loss)
        self._verify_algo_new(stop_ack)

        tp1_ack: AlgoSubmissionAck | None = None
        if protection.take_profit_1 is not None:
            tp1_ack = self.writer.submit_conditional_exit(protection.take_profit_1)
            self._verify_algo_new(tp1_ack)

        tp2_ack = self.writer.submit_conditional_exit(protection.take_profit_2)
        self._verify_algo_new(tp2_ack)
        return protection, stop_ack, tp1_ack, tp2_ack

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
        if readiness.status is not ReadinessStatus.EXECUTION_READY or readiness.geometry is None:
            raise DurableExecutionError("only EXECUTION_READY signals may enter durable execution")
        if not signal_id.startswith("sig-"):
            raise DurableExecutionError("durable execution requires a scanner-generated signal id")
        if readiness.geometry.symbol != instrument.symbol:
            raise DurableExecutionError("instrument does not match durable signal geometry")
        if self.private.get_position_mode_is_hedged():
            raise DurableExecutionError("durable execution requires Binance One-way Mode")

        wallet = self.private.get_wallet_balance()
        positions = self.private.get_positions()
        try:
            plan = build_entry_order_plan(
                readiness,
                signal_id=signal_id,
                wallet=wallet,
                positions=positions,
                instrument=instrument,
                safety=self.safety,
                risk_fraction=risk_fraction,
            )
        except ExecutionPlanError as exc:
            raise DurableExecutionError(str(exc)) from exc

        leverage = _required_leverage(plan, self.safety)
        self.writer.set_leverage(plan.symbol, leverage)
        planned_at_ms = self.now_ms()
        self.linkage.save_entry_plan(
            plan,
            status="PLANNED",
            created_at_ms=planned_at_ms,
            updated_at_ms=planned_at_ms,
        )

        try:
            ack = self.writer.submit_entry(plan)
        except UnknownSubmissionOutcome:
            self.linkage.save_entry_plan(
                plan,
                status="UNKNOWN_OUTCOME",
                updated_at_ms=self.now_ms(),
            )
            raise
        except BinanceOrderSubmissionError:
            self.linkage.save_entry_plan(
                plan,
                status="REJECTED",
                updated_at_ms=self.now_ms(),
            )
            raise

        self.linkage.save_entry_plan(
            plan,
            status="PENDING_RECONCILIATION",
            venue_order_id=ack.order_id,
            created_at_ms=ack.exchange_time_ms or planned_at_ms,
            updated_at_ms=ack.exchange_time_ms or self.now_ms(),
        )

        order = self._reconcile_entry(plan)
        filled_qty = order.cum_exec_qty or Decimal(0)
        if filled_qty <= 0 or filled_qty > plan.qty:
            raise DurableExecutionError("reconciled entry quantity is invalid")

        # Safety before analytics: install exchange-side protection immediately after fill.
        protection, stop_ack, tp1_ack, tp2_ack = self._install_protection(
            plan,
            filled_qty,
            instrument,
        )

        fills = self._entry_fills(order)
        for fill in fills:
            self.linkage.save_fill(fill, client_order_id=plan.order_link_id)
        fill_qty = sum((fill.qty for fill in fills), Decimal(0))
        if fill_qty != filled_qty:
            raise DurableExecutionError(
                f"user-trade fill quantity {fill_qty} does not match order quantity {filled_qty}"
            )
        average_entry_price = order.avg_price or _average_fill_price(fills)
        if average_entry_price <= 0:
            raise DurableExecutionError("reconciled average entry price is invalid")

        open_positions = tuple(
            position
            for position in self.private.get_positions()
            if position.is_open and position.symbol == plan.symbol
        )
        if len(open_positions) != 1:
            raise DurableExecutionError("expected exactly one authoritative open position after fill")
        position = open_positions[0]
        entry_time_ms = min(fill.time_ms for fill in fills)
        position_id = self.linkage.save_open_position(
            plan=plan,
            position=position,
            entry_time_ms=entry_time_ms,
            filled_qty=filled_qty,
            average_entry_price=average_entry_price,
        )
        self.linkage.save_entry_plan(
            plan,
            status="FILLED_PROTECTED",
            venue_order_id=order.order_id,
            avg_price=average_entry_price,
            created_at_ms=order.created_time_ms or entry_time_ms,
            updated_at_ms=order.updated_time_ms or self.now_ms(),
        )

        return DurableExecutionResult(
            signal_id=signal_id,
            position_id=position_id,
            symbol=plan.symbol,
            side=plan.side,
            client_order_id=plan.order_link_id,
            venue_order_id=order.order_id,
            filled_qty=filled_qty,
            average_entry_price=average_entry_price,
            leverage=leverage,
            stop_client_algo_id=stop_ack.client_algo_id,
            tp1_client_algo_id=(tp1_ack.client_algo_id if tp1_ack is not None else None),
            tp2_client_algo_id=tp2_ack.client_algo_id,
        )
