from decimal import Decimal

import pytest

from crypto_scanner.binance.models import OrderSnapshot, PositionSnapshot
from crypto_scanner.binance.private_rest import BinancePrivateApiError
from crypto_scanner.binance.private_write import UnknownSubmissionOutcome
from crypto_scanner.emergency_exit import EmergencyExitError, flatten_scanner_position


def _position(symbol: str = "BTCUSDT") -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        side="Buy",
        size=Decimal("0.01"),
        avg_price=Decimal("100"),
        position_value=Decimal("1"),
        leverage=Decimal("1"),
        mark_price=Decimal("100"),
        liq_price=None,
        unrealised_pnl=Decimal(0),
        cum_realised_pnl=None,
        position_im=None,
        position_mm=None,
        take_profit=None,
        stop_loss=None,
        trailing_stop=None,
        updated_time_ms=1000,
    )


def _exit_order(client_order_id: str, status: str = "FILLED") -> OrderSnapshot:
    return OrderSnapshot(
        order_id="exit-1",
        order_link_id=client_order_id,
        symbol="BTCUSDT",
        side="Sell",
        order_status=status,
        order_type="MARKET",
        time_in_force="GTC",
        price=Decimal(0),
        qty=Decimal("0.01"),
        avg_price=Decimal("100"),
        leaves_qty=Decimal(0),
        cum_exec_qty=Decimal("0.01") if status == "FILLED" else Decimal(0),
        cum_exec_value=Decimal(1),
        cum_exec_fee=None,
        trigger_price=None,
        take_profit=None,
        stop_loss=None,
        reduce_only=True,
        close_on_trigger=False,
        created_time_ms=1000,
        updated_time_ms=1001,
    )


class Reader:
    def __init__(self, *, exit_status: str = "FILLED") -> None:
        self.position_reads = 0
        self.order_reads = 0
        self.exit_status = exit_status

    def get_positions(self):
        self.position_reads += 1
        # Open for admission and for one read-after-fill propagation cycle.
        return (_position(),) if self.position_reads <= 2 else ()

    def get_order_by_client_id(self, symbol: str, client_order_id: str):
        assert symbol == "BTCUSDT"
        assert client_order_id.startswith("cs-panic-")
        self.order_reads += 1
        if self.order_reads == 1:
            raise BinancePrivateApiError("Binance private API error code=-2013 msg=not found")
        return _exit_order(client_order_id, self.exit_status)

    def get_open_algo_orders(self, _symbol=None):
        return ()


class Writer:
    def __init__(self, *, unknown: bool = True) -> None:
        self.submissions = 0
        self.unknown = unknown

    def submit_reduce_only_market_exit(self, **_kwargs):
        self.submissions += 1
        if self.unknown:
            raise UnknownSubmissionOutcome("cs-panic-id", "transport unknown")
        return object()


def test_unknown_emergency_exit_is_reconciled_once_and_flat_state_is_polled() -> None:
    reader = Reader()
    writer = Writer()

    result = flatten_scanner_position(
        reader,
        writer,
        symbol="BTCUSDT",
        management_seed="sig-safe",
        sleep=lambda _seconds: None,
    )

    assert result.exited_qty == Decimal("0.01")
    assert writer.submissions == 1
    assert reader.order_reads == 2
    assert reader.position_reads >= 3


def test_emergency_exit_must_be_authoritatively_filled() -> None:
    reader = Reader(exit_status="REJECTED")

    with pytest.raises(EmergencyExitError, match="did not fill"):
        flatten_scanner_position(
            reader,
            Writer(unknown=False),
            symbol="BTCUSDT",
            management_seed="sig-safe",
            sleep=lambda _seconds: None,
        )


def test_emergency_exit_refuses_ambiguous_positions() -> None:
    class AmbiguousReader(Reader):
        def get_positions(self):
            return (_position(), _position("ETHUSDT"), _position())

    with pytest.raises(EmergencyExitError, match="exactly one"):
        flatten_scanner_position(
            AmbiguousReader(),
            Writer(),
            symbol="BTCUSDT",
            management_seed="sig-safe",
            sleep=lambda _seconds: None,
        )
