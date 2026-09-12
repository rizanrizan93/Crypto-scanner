from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_scanner.binance.models import OrderSnapshot, PositionSnapshot
from crypto_scanner.binance.private_rest import AlgoOrderSnapshot, UserTradeFill
from crypto_scanner.execution_plan import EntryOrderPlan
from crypto_scanner.persistence import SupabasePersistenceConfig
from crypto_scanner.post_fill_recovery import recover_post_fill_failures

NOW = 1_800_000_000_000
SIGNAL_ID = "sig-0123456789abcdef0123456789abcdef"
ORDER_ID = "cs-trxusdt-recovery"


def _plan() -> EntryOrderPlan:
    return EntryOrderPlan(
        signal_id=SIGNAL_ID,
        order_link_id=ORDER_ID,
        symbol="TRXUSDT",
        side="Sell",
        qty=Decimal("39499"),
        entry_price=Decimal("0.30"),
        stop_loss=Decimal("0.302"),
        take_profit_1=Decimal("0.297"),
        take_profit_2=Decimal("0.296"),
        risk_fraction=Decimal("0.005"),
        risk_amount=Decimal("79"),
        notional=Decimal("11849.7"),
        leverage_equivalent=Decimal("2.4"),
    )


def _position() -> PositionSnapshot:
    return PositionSnapshot(
        symbol="TRXUSDT",
        side="Sell",
        size=Decimal("39499"),
        avg_price=Decimal("0.30"),
        position_value=Decimal("11849.7"),
        leverage=Decimal("3"),
        mark_price=Decimal("0.299"),
        liq_price=None,
        unrealised_pnl=Decimal("10"),
        cum_realised_pnl=None,
        position_im=None,
        position_mm=None,
        take_profit=None,
        stop_loss=None,
        trailing_stop=None,
        updated_time_ms=NOW + 4,
    )


def _entry_order(
    *,
    status: str = "FILLED",
    executed: Decimal = Decimal("39499"),
) -> OrderSnapshot:
    return OrderSnapshot(
        order_id="venue-42",
        order_link_id=ORDER_ID,
        symbol="TRXUSDT",
        side="Sell",
        order_status=status,
        order_type="MARKET",
        time_in_force="GTC",
        price=Decimal("0"),
        qty=Decimal("39499"),
        avg_price=Decimal("0.30") if executed > 0 else None,
        leaves_qty=Decimal("39499") - executed,
        cum_exec_qty=executed,
        cum_exec_value=executed * Decimal("0.30"),
        cum_exec_fee=None,
        trigger_price=None,
        take_profit=None,
        stop_loss=None,
        reduce_only=False,
        close_on_trigger=False,
        created_time_ms=NOW + 1,
        updated_time_ms=NOW + 2,
    )


def _fill() -> UserTradeFill:
    return UserTradeFill(
        symbol="TRXUSDT",
        trade_id="fill-7",
        order_id="venue-42",
        side="SELL",
        position_side="BOTH",
        price=Decimal("0.30"),
        qty=Decimal("39499"),
        quote_qty=Decimal("11849.7"),
        realized_pnl=Decimal("0"),
        commission=Decimal("1"),
        commission_asset="USDT",
        buyer=False,
        maker=False,
        time_ms=NOW + 2,
    )


def _history_fill(
    *,
    trade_id: str,
    order_id: str,
    side: str,
    time_ms: int,
) -> UserTradeFill:
    return UserTradeFill(
        symbol="TRXUSDT",
        trade_id=trade_id,
        order_id=order_id,
        side=side,
        position_side="BOTH",
        price=Decimal("0.30"),
        qty=Decimal("39499"),
        quote_qty=Decimal("11849.7"),
        realized_pnl=Decimal("0"),
        commission=Decimal("1"),
        commission_asset="USDT",
        buyer=side == "BUY",
        maker=False,
        time_ms=time_ms,
    )


def _algo(order_type: str, client_id: str, trigger: str) -> AlgoOrderSnapshot:
    return AlgoOrderSnapshot(
        algo_id=f"algo:{client_id}",
        client_algo_id=client_id,
        symbol="TRXUSDT",
        side="BUY",
        order_type=order_type,
        status="NEW",
        trigger_price=Decimal(trigger),
        quantity=Decimal("39499"),
        reduce_only=True,
        updated_time_ms=NOW + 3,
    )


class FakeReader:
    def __init__(
        self,
        *,
        entry_status: str = "FILLED",
        executed: Decimal = Decimal("39499"),
        mark_price: Decimal = Decimal("0.299"),
    ) -> None:
        self.entry_status = entry_status
        self.executed = executed
        self.repaired = False
        self.mark_price = mark_price
        self.flattened = False

    def get_positions(self) -> tuple[PositionSnapshot, ...]:
        if self.flattened:
            return ()
        position = _position()
        return (
            PositionSnapshot(
                symbol=position.symbol,
                side=position.side,
                size=position.size,
                avg_price=position.avg_price,
                position_value=position.position_value,
                leverage=position.leverage,
                mark_price=self.mark_price,
                liq_price=position.liq_price,
                unrealised_pnl=position.unrealised_pnl,
                cum_realised_pnl=position.cum_realised_pnl,
                position_im=position.position_im,
                position_mm=position.position_mm,
                take_profit=position.take_profit,
                stop_loss=position.stop_loss,
                trailing_stop=position.trailing_stop,
                updated_time_ms=position.updated_time_ms,
            ),
        )

    def get_order_by_client_id(self, symbol: str, client_order_id: str) -> OrderSnapshot:
        assert symbol == "TRXUSDT"
        assert client_order_id == ORDER_ID
        return _entry_order(status=self.entry_status, executed=self.executed)

    def get_user_trades(self, symbol: str, **_kwargs: object) -> tuple[UserTradeFill, ...]:
        assert symbol == "TRXUSDT"
        return (_fill(),) if self.executed > 0 else ()

    def get_open_algo_orders(self, symbol: str | None = None) -> tuple[AlgoOrderSnapshot, ...]:
        assert symbol in {None, "TRXUSDT"}
        if not self.repaired:
            return (_algo("STOP_MARKET", "cs-sl-existing", "0.302"),)
        return (
            _algo("STOP_MARKET", "cs-slr-recovered", "0.302"),
            _algo("TAKE_PROFIT_MARKET", "cs-tp2r-recovered", "0.296"),
        )


class FakeLinkage:
    def __init__(self) -> None:
        self.fills: list[UserTradeFill] = []
        self.position_saved = False
        self.statuses: list[str] = []

    def save_fill(self, fill: UserTradeFill, *, client_order_id: str) -> None:
        assert client_order_id == ORDER_ID
        self.fills.append(fill)

    def save_open_position(self, **_kwargs: object) -> str:
        self.position_saved = True
        return "pos-recovered"

    def save_entry_plan(self, _plan: EntryOrderPlan, *, status: str, **_kwargs: object) -> None:
        self.statuses.append(status)


def _config() -> SupabasePersistenceConfig:
    return SupabasePersistenceConfig(
        url="https://abc.supabase.co",
        service_role_key="secret",
    )


def test_pending_row_is_recovered_only_after_authoritative_fill_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = FakeReader()
    linkage = FakeLinkage()
    replacement_calls: list[tuple[str, Decimal, Decimal, str]] = []

    monkeypatch.setattr(
        "crypto_scanner.post_fill_recovery._load_recoverable_plan",
        lambda _config, _symbol: _plan(),
    )

    def replace(_reader, _writer, *, symbol, stop_trigger, tp2_trigger, management_seed):
        replacement_calls.append((symbol, stop_trigger, tp2_trigger, management_seed))
        reader.repaired = True
        return object()

    monkeypatch.setattr(
        "crypto_scanner.post_fill_recovery.replace_aggregate_protection",
        replace,
    )

    result = recover_post_fill_failures(reader, object(), linkage, _config())

    assert result.blockers == ()
    assert result.recovered_protection_symbols == ("TRXUSDT",)
    assert result.recovered_linkage_symbols == ("TRXUSDT",)
    assert result.emergency_flattened_symbols == ()
    assert replacement_calls == [
        ("TRXUSDT", Decimal("0.302"), Decimal("0.296"), SIGNAL_ID)
    ]
    assert len(linkage.fills) == 1
    assert linkage.position_saved
    assert linkage.statuses == ["FILLED_PROTECTED_RECOVERED"]


def test_unfilled_pending_row_never_reaches_protector_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = FakeReader(entry_status="NEW", executed=Decimal("0"))
    linkage = FakeLinkage()
    replacement_called = False

    monkeypatch.setattr(
        "crypto_scanner.post_fill_recovery._load_recoverable_plan",
        lambda _config, _symbol: _plan(),
    )

    def replace(*_args, **_kwargs):
        nonlocal replacement_called
        replacement_called = True
        reader.repaired = True
        return object()

    monkeypatch.setattr(
        "crypto_scanner.post_fill_recovery.replace_aggregate_protection",
        replace,
    )

    result = recover_post_fill_failures(reader, object(), linkage, _config())

    assert replacement_called is False
    assert result.recovered_protection_symbols == ()
    assert result.recovered_linkage_symbols == ()
    assert result.emergency_flattened_symbols == ()
    assert len(result.blockers) == 1
    assert "not authoritatively FILLED" in result.blockers[0]
    assert linkage.fills == []
    assert not linkage.position_saved


def test_stale_recoverable_order_cannot_bind_to_new_same_symbol_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StaleEpisodeReader(FakeReader):
        def get_user_trades(self, symbol: str, **kwargs: object) -> tuple[UserTradeFill, ...]:
            assert symbol == "TRXUSDT"
            history = (
                _history_fill(
                    trade_id="old-entry",
                    order_id="venue-42",
                    side="SELL",
                    time_ms=NOW + 2,
                ),
                _history_fill(
                    trade_id="old-close",
                    order_id="venue-old-close",
                    side="BUY",
                    time_ms=NOW + 20,
                ),
                _history_fill(
                    trade_id="current-entry",
                    order_id="venue-current",
                    side="SELL",
                    time_ms=NOW + 30,
                ),
            )
            if "start_time_ms" in kwargs:
                return history
            return history

    reader = StaleEpisodeReader()
    linkage = FakeLinkage()
    replacement_called = False

    monkeypatch.setattr(
        "crypto_scanner.post_fill_recovery._load_recoverable_plan",
        lambda _config, _symbol: _plan(),
    )

    def replace(*_args, **_kwargs):
        nonlocal replacement_called
        replacement_called = True

    monkeypatch.setattr(
        "crypto_scanner.post_fill_recovery.replace_aggregate_protection",
        replace,
    )

    result = recover_post_fill_failures(reader, object(), linkage, _config())

    assert replacement_called is False
    assert result.recovered_protection_symbols == ()
    assert result.recovered_linkage_symbols == ()
    assert result.emergency_flattened_symbols == ()
    assert len(result.blockers) == 1
    assert "does not match current exchange position episode" in result.blockers[0]
    assert linkage.fills == []
    assert linkage.position_saved is False


def test_stale_recovery_triggers_flatten_instead_of_resubmitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = FakeReader(mark_price=Decimal("0.303"))
    linkage = FakeLinkage()
    replacement_called = False

    monkeypatch.setattr(
        "crypto_scanner.post_fill_recovery._load_recoverable_plan",
        lambda _config, _symbol: _plan(),
    )

    def replace(*_args, **_kwargs):
        nonlocal replacement_called
        replacement_called = True

    def flatten(_reader, _writer, *, symbol, management_seed):
        assert symbol == "TRXUSDT"
        assert management_seed == SIGNAL_ID
        reader.flattened = True
        return object()

    monkeypatch.setattr(
        "crypto_scanner.post_fill_recovery.replace_aggregate_protection",
        replace,
    )
    monkeypatch.setattr(
        "crypto_scanner.post_fill_recovery.flatten_scanner_position",
        flatten,
    )

    result = recover_post_fill_failures(reader, object(), linkage, _config())

    assert replacement_called is False
    assert result.blockers == ()
    assert result.emergency_flattened_symbols == ("TRXUSDT",)
    assert linkage.statuses == ["FILLED_PROTECTION_FAILED_FLATTENED_RECOVERY"]
