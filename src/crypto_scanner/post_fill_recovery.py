from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from crypto_scanner.binance.models import OrderSnapshot, PositionSnapshot
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient, UserTradeFill
from crypto_scanner.binance.private_write import BinanceTestnetOrderClient
from crypto_scanner.execution_plan import EntryOrderPlan
from crypto_scanner.persistence import (
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseRestClient,
)
from crypto_scanner.position_manager import ProtectionStatus, audit_symbol_protection
from crypto_scanner.position_manager_write import (
    PositionManagerError,
    replace_aggregate_protection,
)
from crypto_scanner.trade_linkage import DurableTradeLinkage

_RECOVERABLE_ORDER_STATUSES = (
    "PENDING_RECONCILIATION",
    "FILLED_PROTECTION_FAILED",
    "FILLED_PROTECTION_UNKNOWN",
)


class PostFillRecoveryError(RuntimeError):
    """Raised when a confirmed fill cannot be recovered from durable evidence safely."""


@dataclass(frozen=True, slots=True)
class PostFillRecoveryResult:
    recovered_protection_symbols: tuple[str, ...]
    recovered_linkage_symbols: tuple[str, ...]
    blockers: tuple[str, ...]


class _RecoveryRestClient(SupabaseRestClient):
    def select(self, table: str, *, params: dict[str, str]) -> list[dict[str, object]]:
        if not table.replace("_", "").isalnum():
            raise PersistenceError("invalid recovery table name")
        response = self._client.get(
            f"{self.base_url}/rest/v1/{table}",
            params=params,
            headers=self._headers(),
        )
        if response.is_error:
            raise PersistenceError(
                f"post-fill recovery read failed table={table} status={response.status_code}: "
                f"{response.text[:300]}"
            )
        payload = response.json()
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise PersistenceError(f"post-fill recovery read returned invalid rows: {table}")
        return payload


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (ValueError, TypeError) as exc:
        raise PostFillRecoveryError(f"invalid durable recovery decimal: {field}") from exc
    if result <= 0:
        raise PostFillRecoveryError(f"durable recovery decimal must be positive: {field}")
    return result


def _load_recoverable_plan(
    config: SupabasePersistenceConfig,
    symbol: str,
) -> EntryOrderPlan | None:
    status_filter = "in.(" + ",".join(_RECOVERABLE_ORDER_STATUSES) + ")"
    with _RecoveryRestClient(config) as rest:
        # If durable OPEN linkage already exists for this symbol, bind recovery to that
        # exact signal. A stale recoverable order from an older episode of the same symbol
        # must never be allowed to repair/protect/persist the current exchange position.
        open_rows = rest.select(
            "positions",
            params={
                "select": "position_id,signal_id",
                "venue": "eq.BINANCE",
                "environment": "eq.DEMO",
                "symbol": f"eq.{symbol.upper()}",
                "state": "eq.OPEN",
                "limit": "2",
            },
        )
        if len(open_rows) > 1:
            raise PostFillRecoveryError(
                "multiple durable OPEN position rows exist for recovery symbol"
            )
        active_signal_id: str | None = None
        if open_rows:
            active_signal_id = str(open_rows[0].get("signal_id") or "")
            if not active_signal_id.startswith("sig-"):
                raise PostFillRecoveryError(
                    "durable OPEN position lacks scanner signal identity"
                )

        order_params = {
            "select": (
                "client_order_id,signal_id,symbol,side,status,qty,price,raw,"
                "updated_at_ms"
            ),
            "symbol": f"eq.{symbol.upper()}",
            "order_type": "eq.MARKET",
            "reduce_only": "eq.false",
            "status": status_filter,
            "order": "updated_at_ms.desc",
            "limit": "1",
        }
        if active_signal_id is not None:
            order_params["signal_id"] = f"eq.{active_signal_id}"

        rows = rest.select("orders", params=order_params)
        if not rows:
            return None
        row = rows[0]
        signal_id = str(row.get("signal_id") or "")
        client_order_id = str(row.get("client_order_id") or "")
        side_raw = str(row.get("side") or "").upper()
        if not signal_id.startswith("sig-") or not client_order_id.startswith("cs-"):
            raise PostFillRecoveryError("recoverable order lacks scanner durable identity")
        if active_signal_id is not None and signal_id != active_signal_id:
            raise PostFillRecoveryError(
                "recoverable order signal does not match durable OPEN position signal"
            )
        if side_raw not in {"BUY", "SELL"}:
            raise PostFillRecoveryError("recoverable order side is invalid")
        geometry_rows = rest.select(
            "signal_geometry",
            params={
                "select": "signal_id,entry_price,stop_loss,tp1,tp2",
                "signal_id": f"eq.{signal_id}",
                "limit": "1",
            },
        )
        if len(geometry_rows) != 1:
            raise PostFillRecoveryError("recoverable signal geometry is missing or ambiguous")
        geometry = geometry_rows[0]

    raw = row.get("raw")
    if not isinstance(raw, dict):
        raise PostFillRecoveryError("recoverable entry plan raw risk evidence is missing")
    return EntryOrderPlan(
        signal_id=signal_id,
        order_link_id=client_order_id,
        symbol=symbol.upper(),
        side="Buy" if side_raw == "BUY" else "Sell",
        qty=_decimal(row.get("qty"), "qty"),
        entry_price=_decimal(
            geometry.get("entry_price")
            if geometry.get("entry_price") is not None
            else row.get("price"),
            "entry_price",
        ),
        stop_loss=_decimal(geometry.get("stop_loss"), "stop_loss"),
        take_profit_1=_decimal(geometry.get("tp1"), "tp1"),
        take_profit_2=_decimal(geometry.get("tp2"), "tp2"),
        risk_fraction=_decimal(raw.get("risk_fraction"), "risk_fraction"),
        risk_amount=_decimal(raw.get("risk_amount"), "risk_amount"),
        notional=_decimal(raw.get("notional"), "notional"),
        leverage_equivalent=_decimal(raw.get("leverage_equivalent"), "leverage_equivalent"),
    )


def _entry_fills(
    reader: BinanceDemoPrivateReadOnlyClient,
    plan: EntryOrderPlan,
) -> tuple[OrderSnapshot, tuple[UserTradeFill, ...], Decimal]:
    order = reader.get_order_by_client_id(plan.symbol, plan.order_link_id)
    filled_qty = order.cum_exec_qty or Decimal(0)
    if order.order_status != "FILLED" or filled_qty <= 0:
        raise PostFillRecoveryError(
            f"recoverable entry is not authoritatively FILLED: {order.order_status}"
        )
    start_ms = max(0, (order.created_time_ms or 0) - 60_000)
    fills = tuple(
        fill
        for fill in reader.get_user_trades(plan.symbol, start_time_ms=start_ms, limit=1000)
        if fill.order_id == order.order_id
    )
    if not fills:
        raise PostFillRecoveryError("recoverable entry has no user-trade fills")
    fill_qty = sum((fill.qty for fill in fills), Decimal(0))
    if fill_qty != filled_qty:
        raise PostFillRecoveryError(
            f"recoverable fill quantity mismatch order={filled_qty} fills={fill_qty}"
        )
    quote = sum((fill.price * fill.qty for fill in fills), Decimal(0))
    average_entry = order.avg_price or quote / fill_qty
    if average_entry <= 0:
        raise PostFillRecoveryError("recoverable average entry price is invalid")
    return order, fills, average_entry


def _persist_recovered_linkage(
    linkage: DurableTradeLinkage,
    plan: EntryOrderPlan,
    position: PositionSnapshot,
    order: OrderSnapshot,
    fills: tuple[UserTradeFill, ...],
    average_entry: Decimal,
) -> None:
    for fill in fills:
        linkage.save_fill(fill, client_order_id=plan.order_link_id)
    linkage.save_open_position(
        plan=plan,
        position=position,
        entry_time_ms=min(fill.time_ms for fill in fills),
        filled_qty=sum((fill.qty for fill in fills), Decimal(0)),
        average_entry_price=average_entry,
    )
    linkage.save_entry_plan(
        plan,
        status="FILLED_PROTECTED_RECOVERED",
        venue_order_id=order.order_id,
        avg_price=average_entry,
        created_at_ms=order.created_time_ms or min(fill.time_ms for fill in fills),
        updated_at_ms=order.updated_time_ms or max(fill.time_ms for fill in fills),
    )


def recover_post_fill_failures(
    reader: BinanceDemoPrivateReadOnlyClient,
    writer: BinanceTestnetOrderClient,
    linkage: DurableTradeLinkage,
    persistence_config: SupabasePersistenceConfig,
) -> PostFillRecoveryResult:
    """Repair only scanner fills with durable pending/failed protection evidence.

    A PENDING_RECONCILIATION row is never assumed filled. Before any protector write,
    Binance must prove that exact deterministic entry order is FILLED and its user-trade
    fills must reconcile to the executed quantity. Unsafe or ambiguous protector states
    remain blocking.
    """
    if not persistence_config.enabled:
        raise PostFillRecoveryError("post-fill recovery requires dedicated Supabase")

    recovered_protection: list[str] = []
    recovered_linkage: list[str] = []
    blockers: list[str] = []
    positions = tuple(position for position in reader.get_positions() if position.is_open)

    for position in positions:
        symbol = position.symbol
        try:
            plan = _load_recoverable_plan(persistence_config, symbol)
            if plan is None:
                continue
            expected_side = "Buy" if plan.side == "Buy" else "Sell"
            if position.side != expected_side:
                raise PostFillRecoveryError(
                    f"durable entry side {expected_side} mismatches exchange {position.side}"
                )

            # Authoritative proof of the exact fill must precede every recovery write.
            order, fills, average_entry = _entry_fills(reader, plan)

            active = reader.get_open_algo_orders(symbol)
            report = audit_symbol_protection(symbol, positions, active)
            if report.status in {ProtectionStatus.MISSING_STOP, ProtectionStatus.MISSING_TP}:
                replace_aggregate_protection(
                    reader,
                    writer,
                    symbol=symbol,
                    stop_trigger=plan.stop_loss,
                    tp2_trigger=plan.take_profit_2,
                    management_seed=plan.signal_id,
                )
                recovered_protection.append(symbol)
            elif report.status is not ProtectionStatus.PROTECTED:
                raise PostFillRecoveryError(
                    f"unsafe protector state is not auto-repairable: {report.status.value}"
                )

            refreshed_positions = tuple(
                item
                for item in reader.get_positions()
                if item.is_open and item.symbol == symbol
            )
            if len(refreshed_positions) != 1:
                raise PostFillRecoveryError(
                    "position changed while recovering durable post-fill linkage"
                )
            final_active = reader.get_open_algo_orders(symbol)
            final_report = audit_symbol_protection(symbol, refreshed_positions, final_active)
            if final_report.status is not ProtectionStatus.PROTECTED:
                raise PostFillRecoveryError(
                    f"post-fill recovery final protection audit failed: {final_report.status.value}"
                )
            _persist_recovered_linkage(
                linkage,
                plan,
                refreshed_positions[0],
                order,
                fills,
                average_entry,
            )
            recovered_linkage.append(symbol)
        except (PersistenceError, PositionManagerError, PostFillRecoveryError, RuntimeError) as exc:
            blockers.append(f"POST_FILL_RECOVERY:{symbol}:{type(exc).__name__}:{exc}")

    return PostFillRecoveryResult(
        recovered_protection_symbols=tuple(recovered_protection),
        recovered_linkage_symbols=tuple(recovered_linkage),
        blockers=tuple(blockers),
    )
