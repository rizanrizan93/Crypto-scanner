from __future__ import annotations

import json
from decimal import Decimal

from crypto_scanner.discovery import TradeDirection
from crypto_scanner.stack_execution import _transaction_detail
from crypto_scanner.stack_recovery import _parse_detail, recover_stack_transactions
from crypto_scanner.stack_store import DurableStackState, StackTransaction
from crypto_scanner.stacking import (
    DurableLayer,
    StackClassification,
    StackTransactionState,
)


def _layer(signal_id: str) -> DurableLayer:
    return DurableLayer(
        signal_id=signal_id,
        classification=StackClassification.CONTINUATION_STACK,
        direction=TradeDirection.LONG,
        qty=Decimal("2"),
        entry_price=Decimal("101.25"),
        stop_loss=Decimal("100.50"),
        tp1=Decimal("102.50"),
        tp2=Decimal("104.00"),
        risk_amount=Decimal("1.50"),
        opened_at_ms=1_800_000_000_000,
        client_order_id="cs-entry-new",
    )


def _state(detail: str) -> DurableStackState:
    initial = DurableLayer(
        signal_id="sig-initial",
        classification=StackClassification.INITIAL_ENTRY,
        direction=TradeDirection.LONG,
        qty=Decimal("1"),
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        tp1=Decimal("103"),
        tp2=Decimal("106"),
        risk_amount=Decimal("2"),
        opened_at_ms=1_799_999_000_000,
        client_order_id="cs-entry-initial",
    )
    return DurableStackState(
        symbol="XRPUSDT",
        position_id="pos-xrp",
        direction=TradeDirection.LONG,
        layers=(initial,),
        aggregate_stop_loss=Decimal("99"),
        aggregate_tp2=Decimal("106"),
        stop_client_algo_id="cs-old-stop",
        tp2_client_algo_id="cs-old-tp2",
        transaction=StackTransaction(
            signal_id="sig-new",
            state=StackTransactionState.FILLED,
            started_at_ms=1_800_000_000_000,
            updated_at_ms=1_800_000_000_100,
            old_stop_client_algo_id="cs-old-stop",
            old_tp2_client_algo_id="cs-old-tp2",
            detail=detail,
        ),
        quarantined=True,
        quarantine_reason="RECOVERY_REQUIRED",
    )


class _MissingTickStore:
    def __init__(self, state: DurableStackState) -> None:
        self.state = state
        self.save_called = False

    def load_all(self) -> dict[str, DurableStackState]:
        return {self.state.symbol: self.state}

    def save(self, *_args: object, **_kwargs: object) -> None:
        self.save_called = True


def test_transaction_detail_round_trips_exact_tick_size() -> None:
    pending = _layer("sig-new")
    detail = _transaction_detail(
        pre_position_size=Decimal("1"),
        pending_layer=pending,
        tick_size=Decimal("0.0001"),
        aggregate_stop=Decimal("100.75"),
        aggregate_tp2=Decimal("106.25"),
    )

    pre_size, parsed, tick_size, stop, tp2 = _parse_detail(detail)

    assert pre_size == Decimal("1")
    assert parsed == pending
    assert tick_size == Decimal("0.0001")
    assert stop == Decimal("100.75")
    assert tp2 == Decimal("106.25")


def test_recovery_fails_closed_when_durable_tick_size_is_missing() -> None:
    detail = _transaction_detail(
        pre_position_size=Decimal("1"),
        pending_layer=_layer("sig-new"),
        tick_size=Decimal("0.0001"),
    )
    payload = json.loads(detail)
    payload.pop("tick_size")
    store = _MissingTickStore(_state(json.dumps(payload)))

    result = recover_stack_transactions(
        object(),
        object(),
        store,  # type: ignore[arg-type]
        now_ms=1_800_000_000_500,
    )

    assert result.recovered_symbols == ()
    assert result.cleared_symbols == ()
    assert result.blockers == ("STACK_RECOVERY_MISSING_TICK_SIZE:XRPUSDT",)
    assert not store.save_called
