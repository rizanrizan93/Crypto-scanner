from decimal import Decimal

from crypto_scanner.binance.models import PositionSnapshot
from crypto_scanner.binance.private_rest import AlgoOrderSnapshot
from crypto_scanner.position_manager import ProtectionStatus, audit_symbol_protection


def _position() -> PositionSnapshot:
    return PositionSnapshot(
        symbol="XRPUSDT",
        side="Buy",
        size=Decimal("3.6"),
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


def _algo(order_type: str, qty: str = "3.6") -> AlgoOrderSnapshot:
    return AlgoOrderSnapshot(
        algo_id=f"id-{order_type}",
        client_algo_id=f"client-{order_type}",
        symbol="XRPUSDT",
        side="SELL",
        order_type=order_type,
        status="NEW",
        trigger_price=Decimal("1.4"),
        quantity=Decimal(qty),
        reduce_only=True,
        updated_time_ms=1000,
    )


def test_full_size_stop_and_tp_are_protected() -> None:
    report = audit_symbol_protection(
        "XRPUSDT",
        (_position(),),
        (_algo("STOP_MARKET"), _algo("TAKE_PROFIT_MARKET")),
    )
    assert report.status is ProtectionStatus.PROTECTED
    assert report.block_new_entries is False


def test_missing_stop_blocks_new_entries() -> None:
    report = audit_symbol_protection(
        "XRPUSDT",
        (_position(),),
        (_algo("TAKE_PROFIT_MARKET"),),
    )
    assert report.status is ProtectionStatus.MISSING_STOP
    assert report.block_new_entries is True


def test_quantity_mismatch_blocks_new_entries() -> None:
    report = audit_symbol_protection(
        "XRPUSDT",
        (_position(),),
        (_algo("STOP_MARKET", "3.5"), _algo("TAKE_PROFIT_MARKET")),
    )
    assert report.status is ProtectionStatus.QUANTITY_MISMATCH


def test_flat_symbol_with_active_protector_is_orphaned() -> None:
    report = audit_symbol_protection(
        "XRPUSDT",
        (),
        (_algo("STOP_MARKET"),),
    )
    assert report.status is ProtectionStatus.ORPHAN_PROTECTOR
    assert report.block_new_entries is True
