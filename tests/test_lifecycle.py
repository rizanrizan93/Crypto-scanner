from decimal import Decimal

from crypto_scanner.binance.models import PositionSnapshot, WalletSnapshot
from crypto_scanner.binance.private_ws import (
    ListenKeyExpiredEvent,
    OrderTradeEvent,
)
from crypto_scanner.lifecycle import (
    AuthoritativeLifecycleSnapshot,
    LifecycleState,
    ReconciliationSeverity,
)


def _position(size: str = "3.6") -> PositionSnapshot:
    return PositionSnapshot(
        symbol="XRPUSDT",
        side="Buy",
        size=Decimal(size),
        avg_price=Decimal("1.415"),
        position_value=Decimal("5.094"),
        leverage=Decimal("1"),
        mark_price=Decimal("1.416"),
        liq_price=None,
        unrealised_pnl=Decimal("0.0036"),
        cum_realised_pnl=None,
        position_im=None,
        position_mm=None,
        take_profit=None,
        stop_loss=None,
        trailing_stop=None,
        updated_time_ms=1000,
    )


def _snapshot(size: str = "3.6") -> AuthoritativeLifecycleSnapshot:
    wallet = WalletSnapshot(
        account_type="FUTURES_DEMO",
        total_equity=Decimal("5000"),
        total_wallet_balance=Decimal("5000"),
        total_margin_balance=Decimal("5000"),
        total_available_balance=Decimal("4995"),
        total_perp_upl=Decimal("0"),
        coins=(),
    )
    return AuthoritativeLifecycleSnapshot(
        wallet=wallet,
        positions=(_position(size),),
        open_orders=(),
        open_algo_orders=(),
        hedged_mode=False,
    )


def _fill_event() -> OrderTradeEvent:
    return OrderTradeEvent(
        event_time_ms=1001,
        transaction_time_ms=1000,
        symbol="XRPUSDT",
        client_order_id="cs-xrp-1",
        order_id="123",
        side="BUY",
        position_side="BOTH",
        order_type="MARKET",
        execution_type="TRADE",
        order_status="FILLED",
        original_qty=Decimal("3.6"),
        cumulative_qty=Decimal("3.6"),
        last_filled_qty=Decimal("3.6"),
        last_filled_price=Decimal("1.415"),
        average_price=Decimal("1.415"),
        realized_pnl=Decimal("0"),
        commission=Decimal("0.002"),
        commission_asset="USDT",
        trade_id="77",
        reduce_only=False,
    )


def test_fill_event_is_idempotent() -> None:
    state = LifecycleState()
    event = _fill_event()
    state.apply(event)
    state.apply(event)
    assert len(state.fills) == 1
    assert state.fills[0].trade_id == "77"


def test_seeded_rest_state_has_no_position_drift() -> None:
    snapshot = _snapshot()
    state = LifecycleState()
    state.seed_from_recovery(snapshot)
    assert state.reconcile(snapshot) == ()


def test_position_drift_blocks_execution() -> None:
    state = LifecycleState(positions={"XRPUSDT": Decimal("3.6")})
    issues = state.reconcile(_snapshot("2.0"))
    assert any(issue.code == "POSITION_DRIFT" for issue in issues)
    assert all(
        issue.severity is ReconciliationSeverity.BLOCK
        for issue in issues
        if issue.code == "POSITION_DRIFT"
    )


def test_expired_private_stream_blocks_new_entries() -> None:
    snapshot = _snapshot()
    state = LifecycleState()
    state.seed_from_recovery(snapshot)
    state.apply(ListenKeyExpiredEvent(event_time_ms=2000))
    issues = state.reconcile(snapshot)
    assert any(issue.code == "PRIVATE_STREAM_INVALID" for issue in issues)
