from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_scanner.bybit.models import InstrumentInfo
from crypto_scanner.bybit.private_models import PositionSnapshot, WalletSnapshot
from crypto_scanner.discovery import TradeDirection
from crypto_scanner.execution_plan import (
    ExecutionPlanError,
    build_entry_order_plan,
    deterministic_order_link_id,
)
from crypto_scanner.fast_lane import ReadinessDecision, ReadinessStatus
from crypto_scanner.signal_geometry import EntryMode, SignalGeometry


def _instrument() -> InstrumentInfo:
    return InstrumentInfo(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.1"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_order_qty=Decimal("100"),
        max_market_order_qty=Decimal("50"),
        min_leverage=Decimal("1"),
        max_leverage=Decimal("100"),
        leverage_step=Decimal("0.01"),
    )


def _wallet() -> WalletSnapshot:
    return WalletSnapshot(
        account_type="UNIFIED",
        total_equity=Decimal("1000"),
        total_wallet_balance=Decimal("1000"),
        total_margin_balance=Decimal("1000"),
        total_available_balance=Decimal("1000"),
        total_perp_upl=Decimal("0"),
        coins=(),
    )


def _geometry() -> SignalGeometry:
    return SignalGeometry(
        symbol="BTCUSDT",
        direction=TradeDirection.LONG,
        entry_mode=EntryMode.HL_PULLBACK,
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        take_profit_1=Decimal("103"),
        take_profit_2=Decimal("105"),
        initial_risk=Decimal("2"),
        rr_tp1=Decimal("1.5"),
        rr_tp2=Decimal("2.5"),
        reference_swing=Decimal("98.5"),
        breakout_level=None,
        atr_3m=Decimal("1"),
        chase_atr=Decimal("0"),
    )


def _readiness() -> ReadinessDecision:
    return ReadinessDecision(
        symbol="BTCUSDT",
        status=ReadinessStatus.EXECUTION_READY,
        geometry=_geometry(),
        reasons=("ALL_HARD_GUARDS_PASSED",),
    )


def _position(symbol: str) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        side="Buy",
        size=Decimal("1"),
        avg_price=Decimal("100"),
        position_value=Decimal("100"),
        leverage=Decimal("1"),
        mark_price=Decimal("100"),
        liq_price=None,
        unrealised_pnl=Decimal("0"),
        cum_realised_pnl=Decimal("0"),
        position_im=None,
        position_mm=None,
        take_profit=None,
        stop_loss=None,
        trailing_stop=None,
        updated_time_ms=1,
    )


def test_risk_sizing_uses_equity_stop_distance_and_exchange_step() -> None:
    plan = build_entry_order_plan(
        _readiness(),
        signal_id="signal-2026-09-05-btc-long-1",
        wallet=_wallet(),
        positions=(),
        instrument=_instrument(),
        risk_fraction=Decimal("0.005"),
    )

    assert plan.qty == Decimal("2.500")
    assert plan.risk_amount == Decimal("5.000")
    assert plan.notional == Decimal("250.000")
    assert plan.leverage_equivalent == Decimal("0.250")
    assert plan.side == "Buy"
    assert len(plan.order_link_id) <= 36


def test_order_link_id_is_deterministic_and_signal_specific() -> None:
    first = deterministic_order_link_id("BTCUSDT", "signal-1")
    second = deterministic_order_link_id("BTCUSDT", "signal-1")
    other = deterministic_order_link_id("BTCUSDT", "signal-2")
    assert first == second
    assert first != other


def test_same_symbol_position_fails_closed() -> None:
    with pytest.raises(ExecutionPlanError, match="one-position-per-symbol"):
        build_entry_order_plan(
            _readiness(),
            signal_id="signal-1",
            wallet=_wallet(),
            positions=(_position("BTCUSDT"),),
            instrument=_instrument(),
        )


def test_btc_eth_sol_concentration_guard_limits_third_correlated_position() -> None:
    with pytest.raises(ExecutionPlanError, match="concentration"):
        build_entry_order_plan(
            _readiness(),
            signal_id="signal-1",
            wallet=_wallet(),
            positions=(_position("ETHUSDT"), _position("SOLUSDT")),
            instrument=_instrument(),
        )


def test_risk_above_one_percent_is_rejected() -> None:
    with pytest.raises(ExecutionPlanError, match="risk_fraction"):
        build_entry_order_plan(
            _readiness(),
            signal_id="signal-1",
            wallet=_wallet(),
            positions=(),
            instrument=_instrument(),
            risk_fraction=Decimal("0.011"),
        )
