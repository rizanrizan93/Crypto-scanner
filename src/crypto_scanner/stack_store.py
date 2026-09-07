from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

import httpx

from crypto_scanner.binance.models import PositionSnapshot
from crypto_scanner.discovery import TradeDirection
from crypto_scanner.persistence import (
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseRestClient,
    _without_none,
)
from crypto_scanner.safety import SafetyContract
from crypto_scanner.stacking import (
    DurableLayer,
    StackClassification,
    StackLedgerView,
    StackTransactionState,
)


STACK_STATE_PREFIX = "stack:"
STACK_STATE_VERSION = 1


@dataclass(frozen=True, slots=True)
class StackTransaction:
    signal_id: str
    state: StackTransactionState
    started_at_ms: int
    updated_at_ms: int
    old_stop_client_algo_id: str | None = None
    old_tp2_client_algo_id: str | None = None
    new_stop_client_algo_id: str | None = None
    new_tp2_client_algo_id: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DurableStackState:
    symbol: str
    position_id: str | None
    direction: TradeDirection | None
    layers: tuple[DurableLayer, ...]
    aggregate_stop_loss: Decimal | None = None
    aggregate_tp2: Decimal | None = None
    stop_client_algo_id: str | None = None
    tp2_client_algo_id: str | None = None
    transaction: StackTransaction | None = None
    quarantined: bool = False
    quarantine_reason: str | None = None

    @property
    def ledger(self) -> StackLedgerView:
        return StackLedgerView(
            symbol=self.symbol,
            direction=self.direction,
            layers=self.layers,
            quarantined=self.quarantined,
        )


@dataclass(frozen=True, slots=True)
class SignalRuntimeRecord:
    signal_id: str
    status: str
    expires_at_ms: int | None
    order_count: int

    def is_fresh_unused(self, now_ms: int) -> bool:
        return (
            self.status == "EXECUTION_READY"
            and self.expires_at_ms is not None
            and self.expires_at_ms > now_ms
            and self.order_count == 0
        )


class _StackRestClient(SupabaseRestClient):
    def select(
        self,
        table: str,
        *,
        params: dict[str, str],
    ) -> list[dict[str, object]]:
        if not table.replace("_", "").isalnum():
            raise PersistenceError("invalid persistence table name")
        response = self._client.get(
            f"{self.base_url}/rest/v1/{table}",
            params=params,
            headers=self._headers(),
        )
        if response.is_error:
            raise PersistenceError(
                f"Supabase select failed table={table} status={response.status_code}: "
                f"{response.text[:500]}"
            )
        payload = response.json()
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise PersistenceError(f"Supabase select returned invalid payload for {table}")
        return payload


def _state_key(symbol: str) -> str:
    value = symbol.upper().strip()
    if not value or not value.replace("_", "").isalnum():
        raise PersistenceError("invalid stack symbol")
    return f"{STACK_STATE_PREFIX}{value}"


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - malformed durable state must fail closed
        raise PersistenceError(f"invalid decimal in stack state: {field}") from exc
    return parsed


def _optional_decimal(value: object, field: str) -> Decimal | None:
    return None if value is None else _decimal(value, field)


def _parse_layer(raw: object) -> DurableLayer:
    if not isinstance(raw, dict):
        raise PersistenceError("stack layer must be a JSON object")
    try:
        direction = TradeDirection(str(raw["direction"]))
        classification = StackClassification(str(raw["classification"]))
        return DurableLayer(
            signal_id=str(raw["signal_id"]),
            classification=classification,
            direction=direction,
            qty=_decimal(raw["qty"], "layer.qty"),
            entry_price=_decimal(raw["entry_price"], "layer.entry_price"),
            stop_loss=_decimal(raw["stop_loss"], "layer.stop_loss"),
            tp1=_decimal(raw["tp1"], "layer.tp1"),
            tp2=_decimal(raw["tp2"], "layer.tp2"),
            risk_amount=_decimal(raw["risk_amount"], "layer.risk_amount"),
            opened_at_ms=int(raw["opened_at_ms"]),
            client_order_id=(
                str(raw["client_order_id"]) if raw.get("client_order_id") else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PersistenceError("malformed durable stack layer") from exc


def _parse_transaction(raw: object) -> StackTransaction | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PersistenceError("stack transaction must be a JSON object")
    try:
        return StackTransaction(
            signal_id=str(raw["signal_id"]),
            state=StackTransactionState(str(raw["state"])),
            started_at_ms=int(raw["started_at_ms"]),
            updated_at_ms=int(raw["updated_at_ms"]),
            old_stop_client_algo_id=(
                str(raw["old_stop_client_algo_id"])
                if raw.get("old_stop_client_algo_id")
                else None
            ),
            old_tp2_client_algo_id=(
                str(raw["old_tp2_client_algo_id"])
                if raw.get("old_tp2_client_algo_id")
                else None
            ),
            new_stop_client_algo_id=(
                str(raw["new_stop_client_algo_id"])
                if raw.get("new_stop_client_algo_id")
                else None
            ),
            new_tp2_client_algo_id=(
                str(raw["new_tp2_client_algo_id"])
                if raw.get("new_tp2_client_algo_id")
                else None
            ),
            detail=str(raw["detail"]) if raw.get("detail") else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PersistenceError("malformed durable stack transaction") from exc


def _parse_state(symbol: str, raw: object) -> DurableStackState:
    if not isinstance(raw, dict):
        raise PersistenceError("stack runtime state must be a JSON object")
    raw_symbol = str(raw.get("symbol") or "").upper()
    if raw_symbol != symbol.upper():
        raise PersistenceError("stack runtime state symbol mismatch")
    direction_raw = raw.get("direction")
    direction = TradeDirection(str(direction_raw)) if direction_raw else None
    layers_raw = raw.get("layers") or []
    if not isinstance(layers_raw, list):
        raise PersistenceError("stack layers must be a JSON array")
    layers = tuple(_parse_layer(item) for item in layers_raw)
    return DurableStackState(
        symbol=raw_symbol,
        position_id=str(raw["position_id"]) if raw.get("position_id") else None,
        direction=direction,
        layers=layers,
        aggregate_stop_loss=_optional_decimal(
            raw.get("aggregate_stop_loss"), "aggregate_stop_loss"
        ),
        aggregate_tp2=_optional_decimal(raw.get("aggregate_tp2"), "aggregate_tp2"),
        stop_client_algo_id=(
            str(raw["stop_client_algo_id"]) if raw.get("stop_client_algo_id") else None
        ),
        tp2_client_algo_id=(
            str(raw["tp2_client_algo_id"]) if raw.get("tp2_client_algo_id") else None
        ),
        transaction=_parse_transaction(raw.get("transaction")),
        quarantined=bool(raw.get("quarantined", False)),
        quarantine_reason=(
            str(raw["quarantine_reason"]) if raw.get("quarantine_reason") else None
        ),
    )


def _encode_state(state: DurableStackState) -> dict[str, object]:
    layers = [asdict(layer) for layer in state.layers]
    transaction = asdict(state.transaction) if state.transaction is not None else None
    return _without_none(
        {
            "schema": "crypto-stack-v1",
            "symbol": state.symbol,
            "position_id": state.position_id,
            "direction": state.direction,
            "layers": layers,
            "aggregate_stop_loss": state.aggregate_stop_loss,
            "aggregate_tp2": state.aggregate_tp2,
            "stop_client_algo_id": state.stop_client_algo_id,
            "tp2_client_algo_id": state.tp2_client_algo_id,
            "transaction": transaction,
            "quarantined": state.quarantined,
            "quarantine_reason": state.quarantine_reason,
        }
    )


class DurableStackStore:
    """Durable logical-layer ledger using the existing backend-only runtime_state table."""

    def __init__(
        self,
        config: SupabasePersistenceConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._rest = _StackRestClient(config, client=client)

    def close(self) -> None:
        self._rest.close()

    def __enter__(self) -> DurableStackStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def load(self, symbol: str) -> DurableStackState | None:
        rows = self._rest.select(
            "runtime_state",
            params={
                "select": "state_key,version,state,updated_at_ms",
                "state_key": f"eq.{_state_key(symbol)}",
                "limit": "1",
            },
        )
        if not rows:
            return None
        row = rows[0]
        if int(row.get("version") or 0) != STACK_STATE_VERSION:
            raise PersistenceError("unsupported durable stack state version")
        return _parse_state(symbol.upper(), row.get("state"))

    def load_all(self) -> dict[str, DurableStackState]:
        rows = self._rest.select(
            "runtime_state",
            params={"select": "state_key,version,state,updated_at_ms"},
        )
        states: dict[str, DurableStackState] = {}
        for row in rows:
            key = str(row.get("state_key") or "")
            if not key.startswith(STACK_STATE_PREFIX):
                continue
            if int(row.get("version") or 0) != STACK_STATE_VERSION:
                raise PersistenceError(f"unsupported durable stack state version: {key}")
            symbol = key[len(STACK_STATE_PREFIX) :].upper()
            state = _parse_state(symbol, row.get("state"))
            states[symbol] = state
        return states

    def save(self, state: DurableStackState, *, updated_at_ms: int) -> None:
        if updated_at_ms < 0:
            raise PersistenceError("stack state timestamp cannot be negative")
        self._rest.upsert(
            "runtime_state",
            (
                {
                    "state_key": _state_key(state.symbol),
                    "version": STACK_STATE_VERSION,
                    "state": _encode_state(state),
                    "updated_at_ms": updated_at_ms,
                },
            ),
            on_conflict=("state_key",),
        )

    def signal_runtime_record(self, signal_id: str) -> SignalRuntimeRecord:
        if not signal_id.startswith("sig-"):
            raise PersistenceError("scanner signal id required")
        signals = self._rest.select(
            "signals",
            params={
                "select": "signal_id,status,expires_at_ms",
                "signal_id": f"eq.{signal_id}",
                "limit": "1",
            },
        )
        if len(signals) != 1:
            raise PersistenceError("durable signal record is missing or ambiguous")
        orders = self._rest.select(
            "orders",
            params={
                "select": "client_order_id",
                "signal_id": f"eq.{signal_id}",
            },
        )
        signal = signals[0]
        expires = signal.get("expires_at_ms")
        return SignalRuntimeRecord(
            signal_id=signal_id,
            status=str(signal.get("status") or ""),
            expires_at_ms=int(expires) if expires is not None else None,
            order_count=len(orders),
        )

    def risk_accounting(
        self,
        positions: tuple[PositionSnapshot, ...],
        *,
        equity: Decimal,
        safety: SafetyContract | None = None,
    ) -> tuple[int, int, Decimal]:
        """Return total logical slots, BTC/ETH/SOL slots, and conservative planned risk."""
        safety = safety or SafetyContract()
        safety.validate()
        if equity <= 0:
            raise PersistenceError("equity must be positive for stack risk accounting")
        states = self.load_all()
        total_slots = 0
        correlated_slots = 0
        planned_risk = Decimal(0)
        high_corr = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
        missing_ledger_risk = equity * Decimal(str(safety.max_risk_per_trade))
        for position in positions:
            if not position.is_open:
                continue
            state = states.get(position.symbol)
            if state is None or state.layer_count if False else False:
                pass
            if state is None or not state.layers:
                slots = 1
                risk = missing_ledger_risk
            else:
                if state.direction is not None and state.direction.value != (
                    "LONG" if position.side == "Buy" else "SHORT"
                ):
                    raise PersistenceError(
                        f"stack ledger direction mismatch for {position.symbol}"
                    )
                slots = len(state.layers)
                risk = state.ledger.planned_risk
            total_slots += slots
            planned_risk += risk
            if position.symbol in high_corr:
                correlated_slots += slots
        return total_slots, correlated_slots, planned_risk
