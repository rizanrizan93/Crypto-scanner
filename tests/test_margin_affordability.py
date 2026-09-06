from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_scanner.binance.models import InstrumentInfo, WalletSnapshot
from crypto_scanner.discovery import TradeDirection
from crypto_scanner.durable_execution import _required_leverage
from crypto_scanner.execution_plan import ExecutionPlanError, build_entry_order_plan
from crypto_scanner.fast_lane import ReadinessDecision, ReadinessStatus
from crypto_scanner.safety import SafetyContract
from crypto_scanner.signal_geometry import EntryMode, SignalGeometry


def _instrument() -> InstrumentInfo:
    return InstrumentInfo(
        symbol="ETHUSDT",
        status="Trading",
        contract_type="PERPETUAL",
        base_coin="ETH",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.01"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_order_qty=Decimal("1000"),
        max_market_order_qty=Decimal("1000"),
        min_leverage=None,
        max_leverage=None,
        leverage_step=None,
    )


def _readiness() -> ReadinessDecision:
    geometry = SignalGeometry(
        symbol="ETHUSDT",
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
    return ReadinessDecision(
        symbol="ETHUSDT",
        status=ReadinessStatus.EXECUTION_READY,
        geometry=geometry,
        reasons=("ALL_HARD_GUARDS_PASSED",),
    )


def _wallet(available: Decimal | None) -> WalletSnapshot:
    return WalletSnapshot(
        account_type="FUTURES_DEMO",
        total_equity=Decimal("1000"),
        total_wallet_balance=Decimal("1000"),
        total_margin_balance=Decimal("1000"),
        total_available_balance=available,
        total_perp_upl=Decimal("0"),
        coins=(),
    )


def test_plan_is_capped_by_reserved_available_margin() -> None:
    plan = build_entry_order_plan(
        _readiness(),
        signal_id="sig-margin-cap",
        wallet=_wallet(Decimal("50")),
        positions=(),
        instrument=_instrument(),
        risk_fraction=Decimal("0.005"),
    )

    assert plan.qty == Decimal("1.350")
    assert plan.notional == Decimal("135.000")
    assert plan.risk_amount == Decimal("2.700")
    assert plan.risk_amount < Decimal("5")


def test_required_leverage_accounts_for_reserved_available_margin() -> None:
    plan = build_entry_order_plan(
        _readiness(),
        signal_id="sig-margin-leverage",
        wallet=_wallet(Decimal("50")),
        positions=(),
        instrument=_instrument(),
    )

    assert _required_leverage(
        plan,
        SafetyContract(),
        available_balance=Decimal("50"),
    ) == 3


def test_missing_available_balance_fails_closed() -> None:
    with pytest.raises(ExecutionPlanError, match="available balance"):
        build_entry_order_plan(
            _readiness(),
            signal_id="sig-no-available",
            wallet=_wallet(None),
            positions=(),
            instrument=_instrument(),
        )
