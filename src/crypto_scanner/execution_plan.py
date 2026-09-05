from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from crypto_scanner.binance.models import InstrumentInfo, PositionSnapshot, WalletSnapshot
from crypto_scanner.fast_lane import ReadinessDecision, ReadinessStatus
from crypto_scanner.safety import SafetyContract


class ExecutionPlanError(RuntimeError):
    """Raised when an execution-ready signal cannot be safely sized."""


@dataclass(frozen=True, slots=True)
class TestnetExecutionArm:
    enabled: bool

    @classmethod
    def from_environment(cls) -> TestnetExecutionArm:
        return cls(os.getenv("CRYPTO_SCANNER_TESTNET_EXECUTION", "") == "ENABLED")

    def require_enabled(self) -> None:
        if not self.enabled:
            raise ExecutionPlanError(
                "Testnet order writes are disarmed; "
                "CRYPTO_SCANNER_TESTNET_EXECUTION=ENABLED required"
            )


@dataclass(frozen=True, slots=True)
class EntryOrderPlan:
    signal_id: str
    order_link_id: str
    symbol: str
    side: str
    qty: Decimal
    entry_price: Decimal
    stop_loss: Decimal
    take_profit_1: Decimal
    take_profit_2: Decimal
    risk_fraction: Decimal
    risk_amount: Decimal
    notional: Decimal
    leverage_equivalent: Decimal


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ExecutionPlanError("quantity step must be positive")
    steps = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return steps * step


def deterministic_order_link_id(symbol: str, signal_id: str) -> str:
    if not signal_id.strip():
        raise ExecutionPlanError("signal_id cannot be empty")
    digest = hashlib.sha256(signal_id.encode("utf-8")).hexdigest()[:16]
    value = f"cs-{symbol.lower()}-{digest}"
    if len(value) > 32:
        raise ExecutionPlanError("generated client order id exceeds scanner safety limit")
    return value


def build_entry_order_plan(
    readiness: ReadinessDecision,
    *,
    signal_id: str,
    wallet: WalletSnapshot,
    positions: tuple[PositionSnapshot, ...],
    instrument: InstrumentInfo,
    safety: SafetyContract | None = None,
    risk_fraction: Decimal = Decimal("0.005"),
) -> EntryOrderPlan:
    safety = safety or SafetyContract()
    safety.validate()
    if readiness.status is not ReadinessStatus.EXECUTION_READY or readiness.geometry is None:
        raise ExecutionPlanError("readiness decision is not EXECUTION_READY")
    geometry = readiness.geometry
    if geometry.symbol != instrument.symbol:
        raise ExecutionPlanError("instrument symbol does not match signal geometry")
    if instrument.status != "Trading" or instrument.settle_coin != "USDT":
        raise ExecutionPlanError("instrument is not an active USDT contract")
    if wallet.total_equity is None or wallet.total_equity <= 0:
        raise ExecutionPlanError("authoritative account equity is missing or invalid")

    max_risk = Decimal(str(safety.max_risk_per_trade))
    if not Decimal(0) < risk_fraction <= max_risk:
        raise ExecutionPlanError("risk_fraction exceeds safety contract")

    open_positions = tuple(position for position in positions if position.is_open)
    if any(position.symbol == geometry.symbol for position in open_positions):
        raise ExecutionPlanError("one-position-per-symbol guard rejected order")
    if len(open_positions) >= safety.max_concurrent_positions:
        raise ExecutionPlanError("max concurrent position guard rejected order")

    high_corr = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    if geometry.symbol in high_corr:
        existing_high_corr = sum(position.symbol in high_corr for position in open_positions)
        if existing_high_corr >= 2:
            raise ExecutionPlanError("conservative BTC/ETH/SOL concentration guard rejected order")

    stop_distance = geometry.initial_risk
    if stop_distance <= 0:
        raise ExecutionPlanError("signal stop distance is invalid")

    equity = wallet.total_equity
    risk_amount = equity * risk_fraction
    risk_qty = risk_amount / stop_distance
    leverage_cap = Decimal(str(safety.max_leverage))
    leverage_qty = equity * leverage_cap / geometry.entry_price
    raw_qty = min(risk_qty, leverage_qty)
    qty = _floor_to_step(raw_qty, instrument.qty_step)

    if qty < instrument.min_order_qty:
        raise ExecutionPlanError("risk-sized quantity is below exchange minimum quantity")
    if instrument.max_market_order_qty is not None:
        qty = min(qty, instrument.max_market_order_qty)
        qty = _floor_to_step(qty, instrument.qty_step)

    notional = qty * geometry.entry_price
    if instrument.min_notional_value is not None and notional < instrument.min_notional_value:
        raise ExecutionPlanError("risk-sized order is below exchange minimum notional")
    if qty <= 0 or notional <= 0:
        raise ExecutionPlanError("computed order size is invalid")

    actual_risk = qty * stop_distance
    if actual_risk > risk_amount:
        raise ExecutionPlanError("rounded quantity exceeds requested risk budget")
    leverage_equivalent = notional / equity
    if leverage_equivalent > leverage_cap:
        raise ExecutionPlanError("computed notional exceeds leverage safety cap")

    side = "Buy" if geometry.direction.value == "LONG" else "Sell"
    return EntryOrderPlan(
        signal_id=signal_id,
        order_link_id=deterministic_order_link_id(geometry.symbol, signal_id),
        symbol=geometry.symbol,
        side=side,
        qty=qty,
        entry_price=geometry.entry_price,
        stop_loss=geometry.stop_loss,
        take_profit_1=geometry.take_profit_1,
        take_profit_2=geometry.take_profit_2,
        risk_fraction=risk_fraction,
        risk_amount=actual_risk,
        notional=notional,
        leverage_equivalent=leverage_equivalent,
    )
