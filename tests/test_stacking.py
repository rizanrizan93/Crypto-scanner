from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_scanner.binance.models import PositionSnapshot
from crypto_scanner.discovery import TradeDirection
from crypto_scanner.fast_lane import ReadinessDecision, ReadinessStatus
from crypto_scanner.position_manager import ProtectionReport, ProtectionStatus
from crypto_scanner.safety import SafetyContract
from crypto_scanner.signal_geometry import EntryMode, SignalGeometry
from crypto_scanner.stacking import (
    DurableLayer,
    StackClassification,
    StackLedgerView,
    build_aggregate_protection_geometry,
    evaluate_stack_admission,
)

NOW = 1_800_000_000_000


def _position(*, side: str = "Buy", avg: str = "100", mark: str = "102", pnl: str = "2") -> PositionSnapshot:
    return PositionSnapshot(
        symbol="XRPUSDT",
        side=side,
        size=Decimal("1"),
        avg_price=Decimal(avg),
        position_value=Decimal("100"),
        leverage=Decimal("1"),
        mark_price=Decimal(mark),
        liq_price=None,
        unrealised_pnl=Decimal(pnl),
        cum_realised_pnl=None,
        position_im=None,
        position_mm=None,
        take_profit=None,
        stop_loss=None,
        trailing_stop=None,
        updated_time_ms=NOW,
    )


def _readiness(direction: TradeDirection = TradeDirection.LONG) -> ReadinessDecision:
    long = direction is TradeDirection.LONG
    geometry = SignalGeometry(
        symbol="XRPUSDT",
        direction=direction,
        entry_mode=EntryMode.HL_PULLBACK if long else EntryMode.LH_PULLBACK,
        entry_price=Decimal("102" if long else "98"),
        stop_loss=Decimal("100" if long else "100"),
        take_profit_1=Decimal("105" if long else "95"),
        take_profit_2=Decimal("108" if long else "92"),
        initial_risk=Decimal("2"),
        rr_tp1=Decimal("1.5"),
        rr_tp2=Decimal("3"),
        reference_swing=Decimal("100"),
        breakout_level=None,
        atr_3m=Decimal("1"),
        chase_atr=Decimal("0"),
    )
    return ReadinessDecision(
        symbol="XRPUSDT",
        status=ReadinessStatus.EXECUTION_READY,
        geometry=geometry,
        reasons=("ALL_HARD_GUARDS_PASSED",),
    )


def _layer(signal: str = "sig-old") -> DurableLayer:
    return DurableLayer(
        signal_id=signal,
        classification=StackClassification.INITIAL_ENTRY,
        direction=TradeDirection.LONG,
        qty=Decimal("1"),
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        tp1=Decimal("103"),
        tp2=Decimal("106"),
        risk_amount=Decimal("5"),
        opened_at_ms=NOW - 60_000,
        client_order_id="cs-old",
    )


def _protected() -> ProtectionReport:
    return ProtectionReport(
        symbol="XRPUSDT",
        status=ProtectionStatus.PROTECTED,
        block_new_entries=False,
        detail="protected",
    )


def _admission(
    *,
    position: PositionSnapshot | None = None,
    readiness: ReadinessDecision | None = None,
    signal_id: str = "sig-new",
    layers: tuple[DurableLayer, ...] | None = None,
    total_slots: int = 1,
    correlated_slots: int = 0,
    quarantined: bool = False,
):
    position = position or _position()
    readiness = readiness or _readiness()
    layers = layers if layers is not None else (_layer(),)
    direction = TradeDirection.LONG if position.side == "Buy" else TradeDirection.SHORT
    return evaluate_stack_admission(
        position=position,
        protection=_protected(),
        readiness=readiness,
        signal_id=signal_id,
        signal_expires_at_ms=NOW + 60_000,
        now_ms=NOW,
        ledger=StackLedgerView(
            symbol="XRPUSDT",
            direction=direction,
            layers=layers,
            quarantined=quarantined,
        ),
        total_risk_slots_in_use=total_slots,
        correlated_risk_slots_in_use=correlated_slots,
        portfolio_planned_risk=Decimal("5"),
        equity=Decimal("1000"),
        tick_size=Decimal("0.01"),
        safety=SafetyContract(),
    )


def test_profitable_long_stack_is_allowed() -> None:
    decision = _admission()
    assert decision.allowed
    assert decision.classification is StackClassification.REACCUMULATION_STACK


def test_profitable_short_stack_is_allowed() -> None:
    short_layer = DurableLayer(
        signal_id="sig-old-short",
        classification=StackClassification.INITIAL_ENTRY,
        direction=TradeDirection.SHORT,
        qty=Decimal("1"),
        entry_price=Decimal("100"),
        stop_loss=Decimal("102"),
        tp1=Decimal("97"),
        tp2=Decimal("94"),
        risk_amount=Decimal("5"),
        opened_at_ms=NOW - 60_000,
    )
    decision = _admission(
        position=_position(side="Sell", avg="100", mark="98", pnl="2"),
        readiness=_readiness(TradeDirection.SHORT),
        layers=(short_layer,),
    )
    assert decision.allowed


@pytest.mark.parametrize(
    ("side", "mark", "pnl", "direction"),
    [
        ("Buy", "99", "-1", TradeDirection.LONG),
        ("Sell", "101", "-1", TradeDirection.SHORT),
    ],
)
def test_losing_position_is_rejected(
    side: str,
    mark: str,
    pnl: str,
    direction: TradeDirection,
) -> None:
    layer = _layer() if direction is TradeDirection.LONG else DurableLayer(
        signal_id="sig-old-short",
        classification=StackClassification.INITIAL_ENTRY,
        direction=TradeDirection.SHORT,
        qty=Decimal("1"),
        entry_price=Decimal("100"),
        stop_loss=Decimal("102"),
        tp1=Decimal("97"),
        tp2=Decimal("94"),
        risk_amount=Decimal("5"),
        opened_at_ms=NOW - 60_000,
    )
    decision = _admission(
        position=_position(side=side, avg="100", mark=mark, pnl=pnl),
        readiness=_readiness(direction),
        layers=(layer,),
    )
    assert not decision.allowed
    assert "POSITION_NOT_IN_FLOATING_PROFIT" in decision.reasons


def test_break_even_or_tiny_profit_is_rejected_by_buffer() -> None:
    decision = _admission(position=_position(avg="100", mark="100.1", pnl="0.1"))
    assert not decision.allowed
    assert "INSUFFICIENT_PROFIT_BUFFER" in decision.reasons


def test_opposite_direction_is_rejected() -> None:
    decision = _admission(readiness=_readiness(TradeDirection.SHORT))
    assert not decision.allowed
    assert "OPPOSITE_DIRECTION_FORBIDDEN" in decision.reasons


def test_reused_signal_and_stale_or_quarantined_state_fail_closed() -> None:
    reused = _admission(signal_id="sig-old")
    assert not reused.allowed
    assert "REUSED_SIGNAL" in reused.reasons
    quarantined = _admission(quarantined=True)
    assert not quarantined.allowed
    assert "STACK_QUARANTINED" in quarantined.reasons


def test_three_layer_cap_is_enforced() -> None:
    layers = tuple(
        DurableLayer(
            signal_id=f"sig-{i}",
            classification=StackClassification.CONTINUATION_STACK,
            direction=TradeDirection.LONG,
            qty=Decimal("1"),
            entry_price=Decimal(str(100 + i)),
            stop_loss=Decimal("99"),
            tp1=Decimal("105"),
            tp2=Decimal("108"),
            risk_amount=Decimal("5"),
            opened_at_ms=NOW - i,
        )
        for i in range(3)
    )
    decision = _admission(layers=layers)
    assert not decision.allowed
    assert "LAYER_CAP_REACHED" in decision.reasons


def test_ten_total_logical_slots_are_rejected() -> None:
    decision = _admission(total_slots=10)
    assert not decision.allowed
    assert "MAX_RISK_SLOTS_REACHED" in decision.reasons


def test_high_correlation_bucket_counts_logical_slots() -> None:
    position = _position()
    position = PositionSnapshot(
        **{name: getattr(position, name) for name in position.__dataclass_fields__ if name != "symbol"},
        symbol="BTCUSDT",
    )
    readiness = _readiness()
    geometry = readiness.geometry
    assert geometry is not None
    readiness = ReadinessDecision(
        symbol="BTCUSDT",
        status=readiness.status,
        geometry=SignalGeometry(
            symbol="BTCUSDT",
            direction=geometry.direction,
            entry_mode=geometry.entry_mode,
            entry_price=geometry.entry_price,
            stop_loss=geometry.stop_loss,
            take_profit_1=geometry.take_profit_1,
            take_profit_2=geometry.take_profit_2,
            initial_risk=geometry.initial_risk,
            rr_tp1=geometry.rr_tp1,
            rr_tp2=geometry.rr_tp2,
            reference_swing=geometry.reference_swing,
            breakout_level=geometry.breakout_level,
            atr_3m=geometry.atr_3m,
            chase_atr=geometry.chase_atr,
        ),
        reasons=readiness.reasons,
    )
    decision = evaluate_stack_admission(
        position=position,
        protection=ProtectionReport("BTCUSDT", ProtectionStatus.PROTECTED, False, "ok"),
        readiness=readiness,
        signal_id="sig-new-btc",
        signal_expires_at_ms=NOW + 1,
        now_ms=NOW,
        ledger=StackLedgerView(
            symbol="BTCUSDT",
            direction=TradeDirection.LONG,
            layers=(_layer(),),
        ),
        total_risk_slots_in_use=2,
        correlated_risk_slots_in_use=2,
        portfolio_planned_risk=Decimal("10"),
        equity=Decimal("1000"),
        tick_size=Decimal("0.01"),
        safety=SafetyContract(),
    )
    assert not decision.allowed
    assert "HIGH_CORRELATION_RISK_BUCKET_FULL" in decision.reasons


def test_aggregate_long_stop_locks_profit_and_never_adds_downside_risk() -> None:
    geometry = build_aggregate_protection_geometry(
        direction=TradeDirection.LONG,
        aggregate_qty=Decimal("2"),
        aggregate_entry_price=Decimal("101"),
        mark_price=Decimal("103"),
        old_stop=Decimal("100.5"),
        old_tp2=Decimal("106"),
        new_signal_stop=Decimal("100"),
        new_signal_tp2=Decimal("108"),
        tick_size=Decimal("0.01"),
        layers=(_layer(),),
        new_layer_qty=Decimal("1"),
        new_layer_entry_price=Decimal("102"),
    )
    assert geometry.stop_loss >= Decimal("101.01")
    assert geometry.take_profit_2 == Decimal("108")
    assert geometry.aggregate_risk_amount == Decimal("0.99")
