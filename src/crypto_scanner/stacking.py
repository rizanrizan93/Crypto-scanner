from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from crypto_scanner.binance.models import PositionSnapshot
from crypto_scanner.discovery import TradeDirection
from crypto_scanner.fast_lane import ReadinessDecision, ReadinessStatus
from crypto_scanner.position_manager import ProtectionReport, ProtectionStatus
from crypto_scanner.safety import SafetyContract


class StackClassification(StrEnum):
    INITIAL_ENTRY = "INITIAL_ENTRY"
    REACCUMULATION_STACK = "REACCUMULATION_STACK"
    CONTINUATION_STACK = "CONTINUATION_STACK"


class StackTransactionState(StrEnum):
    PLANNED = "PLANNED"
    ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
    FILLED = "FILLED"
    NEW_STOP_ACTIVE = "NEW_STOP_ACTIVE"
    NEW_TP_ACTIVE = "NEW_TP_ACTIVE"
    OLD_CANCEL_PENDING = "OLD_CANCEL_PENDING"
    PROTECTED = "PROTECTED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class DurableLayer:
    signal_id: str
    classification: StackClassification
    direction: TradeDirection
    qty: Decimal
    entry_price: Decimal
    stop_loss: Decimal
    tp1: Decimal
    tp2: Decimal
    risk_amount: Decimal
    opened_at_ms: int
    client_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class StackLedgerView:
    symbol: str
    direction: TradeDirection | None
    layers: tuple[DurableLayer, ...]
    quarantined: bool = False

    @property
    def layer_count(self) -> int:
        return len(self.layers)

    @property
    def signal_ids(self) -> frozenset[str]:
        return frozenset(layer.signal_id for layer in self.layers)

    @property
    def planned_risk(self) -> Decimal:
        return sum((layer.risk_amount for layer in self.layers), Decimal(0))


@dataclass(frozen=True, slots=True)
class StackAdmission:
    allowed: bool
    classification: StackClassification | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AggregateProtectionGeometry:
    stop_loss: Decimal
    take_profit_2: Decimal
    aggregate_risk_amount: Decimal


_HIGH_CORRELATION_BUCKET = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT"})
_MIN_PROFIT_BUFFER_R = Decimal("0.25")


def direction_for_position(position: PositionSnapshot) -> TradeDirection:
    if position.side == "Buy":
        return TradeDirection.LONG
    if position.side == "Sell":
        return TradeDirection.SHORT
    raise ValueError("open position side must be Buy or Sell")


def favorable_move_per_unit(position: PositionSnapshot) -> Decimal | None:
    if position.avg_price is None or position.mark_price is None:
        return None
    if position.side == "Buy":
        return position.mark_price - position.avg_price
    if position.side == "Sell":
        return position.avg_price - position.mark_price
    return None


def evaluate_stack_admission(
    *,
    position: PositionSnapshot,
    protection: ProtectionReport,
    readiness: ReadinessDecision,
    signal_id: str,
    signal_expires_at_ms: int,
    now_ms: int,
    ledger: StackLedgerView,
    total_risk_slots_in_use: int,
    correlated_risk_slots_in_use: int,
    portfolio_planned_risk: Decimal,
    equity: Decimal,
    tick_size: Decimal,
    safety: SafetyContract | None = None,
) -> StackAdmission:
    """Fail-closed admission for a same-symbol profitable stack layer.

    The scanner only reaches this function after Binance has reported an already-open
    One-way net position. A passing result authorizes sizing/planning, not submission.
    """
    safety = safety or SafetyContract()
    safety.validate()
    reasons: list[str] = []

    if not safety.profitable_stacking_enabled:
        reasons.append("STACKING_DISABLED")
    if not position.is_open:
        reasons.append("NO_OPEN_POSITION")
    if readiness.status is not ReadinessStatus.EXECUTION_READY or readiness.geometry is None:
        reasons.append("NOT_EXECUTION_READY")
    if not signal_id.startswith("sig-"):
        reasons.append("INVALID_SIGNAL_ID")
    if signal_expires_at_ms <= now_ms:
        reasons.append("STALE_SIGNAL")
    if ledger.quarantined:
        reasons.append("STACK_QUARANTINED")
    if signal_id in ledger.signal_ids:
        reasons.append("REUSED_SIGNAL")
    if ledger.layer_count >= safety.max_layers_per_symbol:
        reasons.append("LAYER_CAP_REACHED")
    if total_risk_slots_in_use >= safety.max_concurrent_positions:
        reasons.append("MAX_RISK_SLOTS_REACHED")
    if equity <= 0:
        reasons.append("INVALID_EQUITY")
    if tick_size <= 0:
        reasons.append("INVALID_TICK_SIZE")
    if protection.status is not ProtectionStatus.PROTECTED or protection.block_new_entries:
        reasons.append("EXISTING_POSITION_NOT_PROTECTED")

    if position.unrealised_pnl is None or position.unrealised_pnl <= 0:
        reasons.append("POSITION_NOT_IN_FLOATING_PROFIT")

    geometry = readiness.geometry
    if geometry is not None:
        if geometry.symbol != position.symbol:
            reasons.append("SYMBOL_MISMATCH")
        else:
            position_direction = direction_for_position(position)
            if geometry.direction is not position_direction:
                reasons.append("OPPOSITE_DIRECTION_FORBIDDEN")
            if ledger.direction is not None and ledger.direction is not position_direction:
                reasons.append("LEDGER_DIRECTION_MISMATCH")

            favorable = favorable_move_per_unit(position)
            min_buffer = max(tick_size, geometry.initial_risk * _MIN_PROFIT_BUFFER_R)
            if favorable is None or favorable < min_buffer:
                reasons.append("INSUFFICIENT_PROFIT_BUFFER")

    if position.symbol in _HIGH_CORRELATION_BUCKET:
        if correlated_risk_slots_in_use >= safety.max_high_correlation_risk_slots:
            reasons.append("HIGH_CORRELATION_RISK_BUCKET_FULL")

    max_portfolio_risk = equity * Decimal(str(safety.max_portfolio_risk_fraction))
    if portfolio_planned_risk < 0 or portfolio_planned_risk >= max_portfolio_risk:
        reasons.append("PORTFOLIO_RISK_BUDGET_EXHAUSTED")

    classification: StackClassification | None = None
    if not reasons and geometry is not None:
        classification = (
            StackClassification.REACCUMULATION_STACK
            if geometry.entry_mode.value in {"HL_PULLBACK", "LH_PULLBACK", "REVERSAL"}
            else StackClassification.CONTINUATION_STACK
        )
    return StackAdmission(
        allowed=not reasons,
        classification=classification,
        reasons=tuple(reasons),
    )


def build_aggregate_protection_geometry(
    *,
    direction: TradeDirection,
    aggregate_qty: Decimal,
    aggregate_entry_price: Decimal,
    mark_price: Decimal,
    old_stop: Decimal,
    old_tp2: Decimal,
    new_signal_stop: Decimal,
    new_signal_tp2: Decimal,
    tick_size: Decimal,
    layers: tuple[DurableLayer, ...],
    new_layer_qty: Decimal,
    new_layer_entry_price: Decimal,
) -> AggregateProtectionGeometry:
    """Build a single replacement SL/TP2 for Binance's net One-way position.

    The replacement stop must preserve at least one tick of gross profit on the new
    aggregate average. If that cannot fit strictly between average and mark, stacking
    is rejected instead of loosening protection.
    """
    if aggregate_qty <= 0 or aggregate_entry_price <= 0 or mark_price <= 0:
        raise ValueError("aggregate position geometry must be positive")
    if new_layer_qty <= 0 or new_layer_entry_price <= 0 or tick_size <= 0:
        raise ValueError("new layer/tick geometry must be positive")
    if min(old_stop, old_tp2, new_signal_stop, new_signal_tp2) <= 0:
        raise ValueError("protection triggers must be positive")

    if direction is TradeDirection.LONG:
        stop_floor = aggregate_entry_price + tick_size
        stop_loss = max(old_stop, new_signal_stop, stop_floor)
        if stop_loss >= mark_price - tick_size:
            raise ValueError("aggregate LONG stop cannot preserve profit buffer below mark")
        take_profit_2 = max(old_tp2, new_signal_tp2)
        if take_profit_2 <= mark_price:
            raise ValueError("aggregate LONG TP2 must remain above mark")
    elif direction is TradeDirection.SHORT:
        stop_ceiling = aggregate_entry_price - tick_size
        stop_loss = min(old_stop, new_signal_stop, stop_ceiling)
        if stop_loss <= mark_price + tick_size:
            raise ValueError("aggregate SHORT stop cannot preserve profit buffer above mark")
        take_profit_2 = min(old_tp2, new_signal_tp2)
        if take_profit_2 >= mark_price:
            raise ValueError("aggregate SHORT TP2 must remain below mark")
    else:
        raise ValueError("unsupported aggregate protection direction")

    all_layers = layers + (
        DurableLayer(
            signal_id="pending-layer",
            classification=StackClassification.CONTINUATION_STACK,
            direction=direction,
            qty=new_layer_qty,
            entry_price=new_layer_entry_price,
            stop_loss=new_signal_stop,
            tp1=new_signal_tp2,
            tp2=new_signal_tp2,
            risk_amount=Decimal(0),
            opened_at_ms=0,
        ),
    )
    aggregate_risk = aggregate_risk_at_stop(all_layers, stop_loss, direction)
    return AggregateProtectionGeometry(
        stop_loss=stop_loss,
        take_profit_2=take_profit_2,
        aggregate_risk_amount=aggregate_risk,
    )


def aggregate_risk_at_stop(
    layers: tuple[DurableLayer, ...],
    stop_loss: Decimal,
    direction: TradeDirection,
) -> Decimal:
    if stop_loss <= 0:
        raise ValueError("stop_loss must be positive")
    total = Decimal(0)
    for layer in layers:
        if layer.qty <= 0 or layer.entry_price <= 0:
            raise ValueError("durable layer has invalid quantity or entry")
        if layer.direction is not direction:
            raise ValueError("durable layer direction mismatch")
        if direction is TradeDirection.LONG:
            adverse = max(Decimal(0), layer.entry_price - stop_loss)
        else:
            adverse = max(Decimal(0), stop_loss - layer.entry_price)
        total += layer.qty * adverse
    return total
