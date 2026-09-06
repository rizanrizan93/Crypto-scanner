from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_scanner.binance.models import (
    InstrumentInfo,
    OrderSnapshot,
    PositionSnapshot,
    WalletSnapshot,
)
from crypto_scanner.binance.private_rest import AlgoOrderSnapshot, UserTradeFill
from crypto_scanner.binance.private_write import (
    AlgoSubmissionAck,
    OrderSubmissionAck,
    SubmissionState,
    UnknownSubmissionOutcome,
)
from crypto_scanner.discovery import TradeDirection
from crypto_scanner.durable_execution import DurableExecutionCoordinator, DurableExecutionError
from crypto_scanner.execution_plan import ExecutionPlanError, TestnetExecutionArm
from crypto_scanner.fast_lane import ReadinessDecision, ReadinessStatus
from crypto_scanner.signal_geometry import EntryMode, SignalGeometry

NOW = 1_800_000_000_000
SIGNAL_ID = "sig-0123456789abcdef0123456789abcdef"


def _instrument() -> InstrumentInfo:
    return InstrumentInfo(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="PERPETUAL",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.01"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_order_qty=Decimal("100"),
        max_market_order_qty=Decimal("100"),
        min_leverage=None,
        max_leverage=None,
        leverage_step=None,
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


def _wallet() -> WalletSnapshot:
    return WalletSnapshot(
        account_type="FUTURES_DEMO",
        total_equity=Decimal("1000"),
        total_wallet_balance=Decimal("1000"),
        total_margin_balance=Decimal("1000"),
        total_available_balance=Decimal("1000"),
        total_perp_upl=Decimal("0"),
        coins=(),
    )


def _position() -> PositionSnapshot:
    return PositionSnapshot(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal("2.500"),
        avg_price=Decimal("100"),
        position_value=Decimal("250"),
        leverage=Decimal("1"),
        mark_price=Decimal("100.2"),
        liq_price=None,
        unrealised_pnl=Decimal("0.5"),
        cum_realised_pnl=None,
        position_im=None,
        position_mm=None,
        take_profit=None,
        stop_loss=None,
        trailing_stop=None,
        updated_time_ms=NOW + 4,
    )


def _order() -> OrderSnapshot:
    return OrderSnapshot(
        order_id="42",
        order_link_id="cs-btcusdt-test",
        symbol="BTCUSDT",
        side="Buy",
        order_status="FILLED",
        order_type="MARKET",
        time_in_force="GTC",
        price=Decimal("0"),
        qty=Decimal("2.500"),
        avg_price=Decimal("100"),
        leaves_qty=Decimal("0"),
        cum_exec_qty=Decimal("2.500"),
        cum_exec_value=Decimal("250"),
        cum_exec_fee=None,
        trigger_price=None,
        take_profit=None,
        stop_loss=None,
        reduce_only=False,
        close_on_trigger=False,
        created_time_ms=NOW + 1,
        updated_time_ms=NOW + 2,
    )


def _fill(qty: Decimal = Decimal("2.500")) -> UserTradeFill:
    return UserTradeFill(
        symbol="BTCUSDT",
        trade_id="7",
        order_id="42",
        side="BUY",
        position_side="BOTH",
        price=Decimal("100"),
        qty=qty,
        quote_qty=qty * Decimal("100"),
        realized_pnl=Decimal("0"),
        commission=Decimal("0.01"),
        commission_asset="USDT",
        buyer=True,
        maker=False,
        time_ms=NOW + 2,
    )


class FakePrivate:
    def __init__(self, *, fill_qty: Decimal = Decimal("2.500")) -> None:
        self.position_reads = 0
        self.order_reads = 0
        self.fill_qty = fill_qty

    def get_position_mode_is_hedged(self) -> bool:
        return False

    def get_wallet_balance(self) -> WalletSnapshot:
        return _wallet()

    def get_positions(self) -> tuple[PositionSnapshot, ...]:
        self.position_reads += 1
        return () if self.position_reads == 1 else (_position(),)

    def get_order_by_client_id(self, symbol: str, client_order_id: str) -> OrderSnapshot:
        assert symbol == "BTCUSDT"
        assert client_order_id.startswith("cs-btcusdt-")
        self.order_reads += 1
        return _order()

    def get_user_trades(self, symbol: str, **_kwargs: object) -> tuple[UserTradeFill, ...]:
        assert symbol == "BTCUSDT"
        return (_fill(self.fill_qty),)

    def get_algo_order_by_client_id(self, client_algo_id: str) -> AlgoOrderSnapshot:
        return AlgoOrderSnapshot(
            algo_id=f"algo:{client_algo_id}",
            client_algo_id=client_algo_id,
            symbol="BTCUSDT",
            side="SELL",
            order_type=("STOP_MARKET" if "-sl-" in client_algo_id else "TAKE_PROFIT_MARKET"),
            status="NEW",
            trigger_price=Decimal("98"),
            quantity=Decimal("2.500"),
            reduce_only=True,
            updated_time_ms=NOW + 3,
        )


class FakeWriter:
    def __init__(self, *, unknown_entry: bool = False) -> None:
        self.calls: list[str] = []
        self.unknown_entry = unknown_entry

    def set_leverage(self, symbol: str, leverage: int) -> int:
        self.calls.append(f"leverage:{symbol}:{leverage}")
        return leverage

    def submit_entry(self, plan) -> OrderSubmissionAck:
        self.calls.append("entry")
        if self.unknown_entry:
            raise UnknownSubmissionOutcome(plan.order_link_id, "unknown")
        return OrderSubmissionAck(
            order_id="42",
            client_order_id=plan.order_link_id,
            state=SubmissionState.PENDING_RECONCILIATION,
            exchange_time_ms=NOW + 1,
        )

    def submit_stop_loss(self, protection) -> AlgoSubmissionAck:
        self.calls.append("STOP_MARKET")
        return AlgoSubmissionAck(
            algo_id="algo-stop",
            client_algo_id=protection.stop_client_algo_id,
            state=SubmissionState.PENDING_RECONCILIATION,
            exchange_time_ms=NOW + 3,
        )

    def submit_take_profit(self, protection) -> AlgoSubmissionAck:
        self.calls.append("TAKE_PROFIT_MARKET")
        return AlgoSubmissionAck(
            algo_id="algo-tp2",
            client_algo_id=protection.take_profit_client_algo_id,
            state=SubmissionState.PENDING_RECONCILIATION,
            exchange_time_ms=NOW + 3,
        )


class FakeLinkage:
    def __init__(self) -> None:
        self.order_statuses: list[str] = []
        self.fills: list[UserTradeFill] = []
        self.position_saved = False

    def save_entry_plan(self, _plan, *, status: str, **_kwargs: object) -> None:
        self.order_statuses.append(status)

    def save_fill(self, fill: UserTradeFill, *, client_order_id: str) -> None:
        assert client_order_id.startswith("cs-btcusdt-")
        self.fills.append(fill)

    def save_open_position(self, **_kwargs: object) -> str:
        self.position_saved = True
        return "pos-durable-test"


def _coordinator(
    *,
    arm: TestnetExecutionArm,
    private: FakePrivate | None = None,
    writer: FakeWriter | None = None,
    linkage: FakeLinkage | None = None,
) -> DurableExecutionCoordinator:
    return DurableExecutionCoordinator(
        private=private or FakePrivate(),
        writer=writer or FakeWriter(),
        linkage=linkage or FakeLinkage(),
        arm=arm,
        sleep=lambda _seconds: None,
        now_ms=lambda: NOW,
    )


def test_disarmed_coordinator_stops_before_any_private_or_write_call() -> None:
    private = FakePrivate()
    writer = FakeWriter()
    linkage = FakeLinkage()
    coordinator = _coordinator(
        arm=TestnetExecutionArm(False),
        private=private,
        writer=writer,
        linkage=linkage,
    )

    with pytest.raises(ExecutionPlanError, match="disarmed"):
        coordinator.execute(_readiness(), signal_id=SIGNAL_ID, instrument=_instrument())

    assert private.position_reads == 0
    assert private.order_reads == 0
    assert writer.calls == []
    assert linkage.order_statuses == []


def test_success_persists_identity_and_installs_full_size_stop_then_tp2() -> None:
    private = FakePrivate()
    writer = FakeWriter()
    linkage = FakeLinkage()
    coordinator = _coordinator(
        arm=TestnetExecutionArm(True),
        private=private,
        writer=writer,
        linkage=linkage,
    )

    result = coordinator.execute(_readiness(), signal_id=SIGNAL_ID, instrument=_instrument())

    assert result.position_id == "pos-durable-test"
    assert result.signal_id == SIGNAL_ID
    assert result.filled_qty == Decimal("2.500")
    assert result.average_entry_price == Decimal("100")
    assert result.tp1_client_algo_id is None
    assert linkage.order_statuses == [
        "PLANNED",
        "PENDING_RECONCILIATION",
        "FILLED_PROTECTED",
    ]
    assert len(linkage.fills) == 1
    assert linkage.position_saved
    assert writer.calls[0].startswith("leverage:BTCUSDT:")
    assert writer.calls[1] == "entry"
    assert writer.calls[2:] == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]


def test_unknown_entry_outcome_is_persisted_and_never_retried() -> None:
    private = FakePrivate()
    writer = FakeWriter(unknown_entry=True)
    linkage = FakeLinkage()
    coordinator = _coordinator(
        arm=TestnetExecutionArm(True),
        private=private,
        writer=writer,
        linkage=linkage,
    )

    with pytest.raises(UnknownSubmissionOutcome):
        coordinator.execute(_readiness(), signal_id=SIGNAL_ID, instrument=_instrument())

    assert writer.calls.count("entry") == 1
    assert private.order_reads == 0
    assert linkage.order_statuses == ["PLANNED", "UNKNOWN_OUTCOME"]
    assert not linkage.position_saved


def test_fill_quantity_mismatch_fails_before_position_is_marked_durable() -> None:
    private = FakePrivate(fill_qty=Decimal("2.000"))
    writer = FakeWriter()
    linkage = FakeLinkage()
    coordinator = _coordinator(
        arm=TestnetExecutionArm(True),
        private=private,
        writer=writer,
        linkage=linkage,
    )

    with pytest.raises(DurableExecutionError, match="does not match order quantity"):
        coordinator.execute(_readiness(), signal_id=SIGNAL_ID, instrument=_instrument())

    assert writer.calls[2:] == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    assert not linkage.position_saved
    assert "FILLED_PROTECTED" not in linkage.order_statuses


def test_non_scanner_signal_id_is_rejected_before_exchange_reads() -> None:
    private = FakePrivate()
    coordinator = _coordinator(arm=TestnetExecutionArm(True), private=private)

    with pytest.raises(DurableExecutionError, match="scanner-generated signal id"):
        coordinator.execute(_readiness(), signal_id="manual-smoke", instrument=_instrument())

    assert private.position_reads == 0
