from __future__ import annotations

import json
from dataclasses import dataclass, replace
from decimal import Decimal

from crypto_scanner.binance.private_rest import (
    BinanceDemoPrivateReadOnlyClient,
    BinancePrivateApiError,
)
from crypto_scanner.binance.private_write import (
    BinanceTestnetOrderClient,
    UnknownSubmissionOutcome,
    deterministic_management_id,
)
from crypto_scanner.discovery import TradeDirection
from crypto_scanner.position_manager_write import (
    PositionManagerError,
    replace_aggregate_protection,
)
from crypto_scanner.stack_store import DurableStackState, DurableStackStore
from crypto_scanner.stacking import (
    DurableLayer,
    StackClassification,
    StackTransactionState,
    build_aggregate_protection_geometry,
)


@dataclass(frozen=True, slots=True)
class StackRecoveryResult:
    recovered_symbols: tuple[str, ...]
    cleared_symbols: tuple[str, ...]
    blockers: tuple[str, ...]


def _decimal(value: object, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid recovery decimal: {field}") from exc


def _parse_detail(detail: str | None) -> tuple[Decimal, DurableLayer, Decimal | None, Decimal | None]:
    if not detail:
        raise ValueError("stack transaction detail is missing")
    raw = json.loads(detail)
    if not isinstance(raw, dict) or not isinstance(raw.get("pending_layer"), dict):
        raise ValueError("stack transaction detail is malformed")
    layer_raw = raw["pending_layer"]
    layer = DurableLayer(
        signal_id=str(layer_raw["signal_id"]),
        classification=StackClassification(str(layer_raw["classification"])),
        direction=TradeDirection(str(layer_raw["direction"])),
        qty=_decimal(layer_raw["qty"], "pending.qty"),
        entry_price=_decimal(layer_raw["entry_price"], "pending.entry_price"),
        stop_loss=_decimal(layer_raw["stop_loss"], "pending.stop_loss"),
        tp1=_decimal(layer_raw["tp1"], "pending.tp1"),
        tp2=_decimal(layer_raw["tp2"], "pending.tp2"),
        risk_amount=_decimal(layer_raw["risk_amount"], "pending.risk_amount"),
        opened_at_ms=int(layer_raw["opened_at_ms"]),
        client_order_id=(
            str(layer_raw["client_order_id"]) if layer_raw.get("client_order_id") else None
        ),
    )
    return (
        _decimal(raw["pre_position_size"], "pre_position_size"),
        layer,
        _decimal(raw["aggregate_stop"], "aggregate_stop")
        if raw.get("aggregate_stop") is not None
        else None,
        _decimal(raw["aggregate_tp2"], "aggregate_tp2")
        if raw.get("aggregate_tp2") is not None
        else None,
    )


def _finish_state(
    store: DurableStackStore,
    state: DurableStackState,
    layer: DurableLayer,
    *,
    stop: Decimal,
    tp2: Decimal,
    updated_at_ms: int,
) -> None:
    layers = state.layers if layer.signal_id in state.ledger.signal_ids else state.layers + (layer,)
    store.save(
        replace(
            state,
            layers=layers,
            aggregate_stop_loss=stop,
            aggregate_tp2=tp2,
            stop_client_algo_id=deterministic_management_id(state.symbol, layer.signal_id, "slr"),
            tp2_client_algo_id=deterministic_management_id(state.symbol, layer.signal_id, "tp2r"),
            transaction=None,
            quarantined=False,
            quarantine_reason=None,
        ),
        updated_at_ms=updated_at_ms,
    )


def recover_stack_transactions(
    reader: BinanceDemoPrivateReadOnlyClient,
    writer: BinanceTestnetOrderClient,
    store: DurableStackStore,
    *,
    now_ms: int,
) -> StackRecoveryResult:
    """Recover only transactions with durable deterministic identities.

    No write is retried merely because a previous call was uncertain. The current
    Binance position/order/protector state is read first; a write is issued only when
    authoritative state proves that the intended deterministic object is absent/stale.
    """
    recovered: list[str] = []
    cleared: list[str] = []
    blockers: list[str] = []
    states = store.load_all()

    for symbol, state in sorted(states.items()):
        tx = state.transaction
        if tx is None:
            continue
        try:
            pre_size, pending, aggregate_stop, aggregate_tp2 = _parse_detail(tx.detail)
            if pending.signal_id != tx.signal_id or pending.client_order_id is None:
                raise ValueError("pending layer identity does not match transaction")

            positions = tuple(
                position
                for position in reader.get_positions()
                if position.is_open and position.symbol == symbol
            )
            if len(positions) > 1:
                raise ValueError("multiple net positions violate One-way Mode")

            order = None
            try:
                order = reader.get_order_by_client_id(symbol, pending.client_order_id)
            except BinancePrivateApiError:
                # If protection replacement was already underway, the durable fill
                # state is sufficient. Otherwise an unknown entry must remain quarantined.
                if aggregate_stop is None or aggregate_tp2 is None:
                    blockers.append(f"STACK_ENTRY_RECONCILIATION_UNCERTAIN:{symbol}")
                    continue

            if order is not None and (order.cum_exec_qty or Decimal(0)) <= 0:
                if order.order_status in {"CANCELED", "EXPIRED", "REJECTED"}:
                    store.save(
                        replace(
                            state,
                            transaction=None,
                            quarantined=False,
                            quarantine_reason=None,
                        ),
                        updated_at_ms=now_ms,
                    )
                    cleared.append(symbol)
                    continue
                blockers.append(f"STACK_ENTRY_STILL_UNRESOLVED:{symbol}")
                continue

            if not positions:
                blockers.append(f"STACK_TRANSACTION_POSITION_FLAT:{symbol}")
                continue
            position = positions[0]
            if position.side != ("Buy" if pending.direction is TradeDirection.LONG else "Sell"):
                raise ValueError("recovered stack direction mismatches exchange position")

            if order is not None:
                filled_qty = order.cum_exec_qty or Decimal(0)
                if filled_qty <= 0 or position.size != pre_size + filled_qty:
                    raise ValueError("recovered stack quantity does not reconcile")
                fills = tuple(
                    fill
                    for fill in reader.get_user_trades(
                        symbol,
                        start_time_ms=max(0, (order.created_time_ms or tx.started_at_ms) - 60_000),
                        limit=1000,
                    )
                    if fill.order_id == order.order_id
                )
                if not fills:
                    raise ValueError("recovered stack entry fills are missing")
                fill_qty = sum((fill.qty for fill in fills), Decimal(0))
                if fill_qty != filled_qty:
                    raise ValueError("recovered stack fill quantity mismatch")
                quote = sum((fill.price * fill.qty for fill in fills), Decimal(0))
                actual_entry = order.avg_price or quote / fill_qty
                pending = replace(
                    pending,
                    qty=filled_qty,
                    entry_price=actual_entry,
                    risk_amount=filled_qty * abs(actual_entry - pending.stop_loss),
                    opened_at_ms=min(fill.time_ms for fill in fills),
                )

            if aggregate_stop is None or aggregate_tp2 is None:
                if position.avg_price is None or position.mark_price is None:
                    raise ValueError("recovery position lacks average/mark price")
                active = tuple(
                    order
                    for order in reader.get_open_algo_orders(symbol)
                    if order.status.upper() in {"NEW", "PENDING", "WORKING"}
                )
                old_stop = next(
                    (order for order in active if order.client_algo_id == tx.old_stop_client_algo_id),
                    None,
                )
                old_tp2 = next(
                    (order for order in active if order.client_algo_id == tx.old_tp2_client_algo_id),
                    None,
                )
                if (
                    old_stop is None
                    or old_tp2 is None
                    or old_stop.trigger_price is None
                    or old_tp2.trigger_price is None
                ):
                    raise ValueError("old durable protectors are unavailable for recovery")
                geometry = build_aggregate_protection_geometry(
                    direction=pending.direction,
                    aggregate_qty=position.size,
                    aggregate_entry_price=position.avg_price,
                    mark_price=position.mark_price,
                    old_stop=old_stop.trigger_price,
                    old_tp2=old_tp2.trigger_price,
                    new_signal_stop=pending.stop_loss,
                    new_signal_tp2=pending.tp2,
                    tick_size=max(abs(pending.entry_price - pending.stop_loss) / Decimal("1000"), Decimal("0.00000001")),
                    layers=state.layers,
                    new_layer_qty=pending.qty,
                    new_layer_entry_price=pending.entry_price,
                )
                aggregate_stop = geometry.stop_loss
                aggregate_tp2 = geometry.take_profit_2

            try:
                replace_aggregate_protection(
                    reader,
                    writer,
                    symbol=symbol,
                    stop_trigger=aggregate_stop,
                    tp2_trigger=aggregate_tp2,
                    management_seed=tx.signal_id,
                )
            except (UnknownSubmissionOutcome, PositionManagerError):
                blockers.append(f"STACK_PROTECTION_RECOVERY_UNCERTAIN:{symbol}")
                continue

            _finish_state(
                store,
                state,
                pending,
                stop=aggregate_stop,
                tp2=aggregate_tp2,
                updated_at_ms=now_ms,
            )
            recovered.append(symbol)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(f"STACK_RECOVERY_INVALID_STATE:{symbol}:{exc}")

    return StackRecoveryResult(
        recovered_symbols=tuple(recovered),
        cleared_symbols=tuple(cleared),
        blockers=tuple(blockers),
    )
