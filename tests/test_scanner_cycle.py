from __future__ import annotations

from decimal import Decimal

from crypto_scanner.binance.models import PositionSnapshot, WalletSnapshot
from crypto_scanner.binance.private_rest import AlgoOrderSnapshot
from crypto_scanner.lifecycle import AuthoritativeLifecycleSnapshot
from crypto_scanner.safety import SafetyContract
from crypto_scanner.scanner_cycle import (
    candidate_account_skip_reason,
    evaluate_account_execution_gate,
)


def _wallet() -> WalletSnapshot:
    return WalletSnapshot(
        account_type="FUTURES_DEMO",
        total_equity=Decimal("5000"),
        total_wallet_balance=Decimal("5000"),
        total_margin_balance=Decimal("5000"),
        total_available_balance=Decimal("4990"),
        total_perp_upl=Decimal("0"),
        coins=(),
    )


def _position(symbol: str, qty: str = "1") -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        side="Buy",
        size=Decimal(qty),
        avg_price=Decimal("100"),
        position_value=Decimal("100"),
        leverage=Decimal("1"),
        mark_price=Decimal("102"),
        liq_price=None,
        unrealised_pnl=Decimal("2"),
        cum_realised_pnl=None,
        position_im=None,
        position_mm=None,
        take_profit=None,
        stop_loss=None,
        trailing_stop=None,
        updated_time_ms=1_000,
    )


def _algo(symbol: str, order_type: str, qty: str = "1") -> AlgoOrderSnapshot:
    return AlgoOrderSnapshot(
        algo_id=f"{symbol}-{order_type}",
        client_algo_id=f"cs-{symbol}-{order_type}",
        symbol=symbol,
        side="SELL",
        order_type=order_type,
        status="NEW",
        trigger_price=Decimal("99" if order_type == "STOP_MARKET" else "105"),
        quantity=Decimal(qty),
        reduce_only=True,
        updated_time_ms=1_000,
    )


def _snapshot(
    *,
    positions: tuple[PositionSnapshot, ...] = (),
    algos: tuple[AlgoOrderSnapshot, ...] = (),
    hedged: bool = False,
) -> AuthoritativeLifecycleSnapshot:
    return AuthoritativeLifecycleSnapshot(
        wallet=_wallet(),
        positions=positions,
        open_orders=(),
        open_algo_orders=algos,
        hedged_mode=hedged,
    )


def test_protected_existing_position_does_not_globally_block_new_entries() -> None:
    snapshot = _snapshot(
        positions=(_position("XRPUSDT", "3.6"),),
        algos=(
            _algo("XRPUSDT", "STOP_MARKET", "3.6"),
            _algo("XRPUSDT", "TAKE_PROFIT_MARKET", "3.6"),
        ),
    )
    gate = evaluate_account_execution_gate(snapshot, SafetyContract())
    assert not gate.blocked
    assert gate.reasons == ()
    assert gate.open_position_symbols == ("XRPUSDT",)


def test_missing_take_profit_blocks_execution_before_candidate_selection() -> None:
    snapshot = _snapshot(
        positions=(_position("XRPUSDT", "3.6"),),
        algos=(_algo("XRPUSDT", "STOP_MARKET", "3.6"),),
    )
    gate = evaluate_account_execution_gate(snapshot, SafetyContract())
    assert gate.blocked
    assert "PROTECTION_MISSING_TP:XRPUSDT" in gate.reasons


def test_hedge_mode_blocks_execution() -> None:
    gate = evaluate_account_execution_gate(_snapshot(hedged=True), SafetyContract())
    assert gate.blocked
    assert "HEDGE_MODE_FORBIDDEN:*" in gate.reasons


def test_candidate_filter_allows_same_symbol_to_reach_stack_specific_gates() -> None:
    snapshot = _snapshot(positions=(_position("XRPUSDT"),))
    assert candidate_account_skip_reason("XRPUSDT", snapshot, SafetyContract()) is None


def test_candidate_filter_blocks_high_correlation_bucket_by_logical_slots() -> None:
    snapshot = _snapshot(positions=(_position("BTCUSDT"),))
    assert (
        candidate_account_skip_reason(
            "SOLUSDT",
            snapshot,
            SafetyContract(),
            risk_slots_in_use=2,
            correlated_risk_slots_in_use=2,
        )
        == "HIGH_CORRELATION_RISK_BUCKET_FULL"
    )


def test_candidate_filter_blocks_when_ten_logical_slots_already_reached() -> None:
    snapshot = _snapshot(positions=(_position("XRPUSDT"),))
    assert (
        candidate_account_skip_reason(
            "ETHUSDT",
            snapshot,
            SafetyContract(),
            risk_slots_in_use=10,
            correlated_risk_slots_in_use=0,
        )
        == "MAX_RISK_SLOTS_REACHED"
    )
