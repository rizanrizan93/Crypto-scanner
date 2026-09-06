from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from crypto_scanner.binance.models import OrderSnapshot, PositionSnapshot, WalletSnapshot
from crypto_scanner.binance.private_rest import (
    AlgoOrderSnapshot,
    BinanceDemoPrivateReadOnlyClient,
)
from crypto_scanner.binance.private_ws import (
    AccountUpdateEvent,
    ListenKeyExpiredEvent,
    MarginCallEvent,
    OrderTradeEvent,
    OtherPrivateEvent,
    PrivateEvent,
)


class LifecycleError(RuntimeError):
    """Raised when exchange lifecycle state violates a scanner invariant."""


class ReconciliationSeverity(StrEnum):
    INFO = "INFO"
    BLOCK = "BLOCK"


@dataclass(frozen=True, slots=True)
class FillEvidence:
    symbol: str
    order_id: str
    client_order_id: str
    trade_id: str
    side: str
    qty: Decimal
    price: Decimal
    realized_pnl: Decimal
    commission: Decimal | None
    commission_asset: str | None
    transaction_time_ms: int


@dataclass(frozen=True, slots=True)
class ReconciliationIssue:
    severity: ReconciliationSeverity
    code: str
    symbol: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class AuthoritativeLifecycleSnapshot:
    wallet: WalletSnapshot
    positions: tuple[PositionSnapshot, ...]
    open_orders: tuple[OrderSnapshot, ...]
    open_algo_orders: tuple[AlgoOrderSnapshot, ...]
    hedged_mode: bool

    @property
    def open_positions(self) -> tuple[PositionSnapshot, ...]:
        return tuple(position for position in self.positions if position.is_open)


def recover_authoritative_state(
    client: BinanceDemoPrivateReadOnlyClient,
) -> AuthoritativeLifecycleSnapshot:
    """Recover current state after process/job restart. Binance is authoritative."""
    return AuthoritativeLifecycleSnapshot(
        wallet=client.get_wallet_balance(),
        positions=client.get_positions(),
        open_orders=client.get_open_orders(),
        open_algo_orders=client.get_open_algo_orders(),
        hedged_mode=client.get_position_mode_is_hedged(),
    )


@dataclass(slots=True)
class LifecycleState:
    """In-memory event state that can always be rebuilt from authoritative REST state."""

    positions: dict[str, Decimal] = field(default_factory=dict)
    orders: dict[tuple[str, str], OrderTradeEvent] = field(default_factory=dict)
    fills: list[FillEvidence] = field(default_factory=list)
    stream_valid: bool = True
    margin_call_seen: bool = False
    last_event_time_ms: int | None = None
    _seen_fill_keys: set[tuple[str, str, str]] = field(default_factory=set)

    def seed_from_recovery(self, snapshot: AuthoritativeLifecycleSnapshot) -> None:
        self.positions = {
            position.symbol: (
                position.size if position.side == "Buy" else -position.size
                if position.side == "Sell"
                else Decimal(0)
            )
            for position in snapshot.positions
        }
        self.stream_valid = True
        self.margin_call_seen = False

    def apply(self, event: PrivateEvent) -> None:
        event_time_ms = getattr(event, "event_time_ms", None)
        if event_time_ms is not None:
            self.last_event_time_ms = max(self.last_event_time_ms or 0, event_time_ms)

        if isinstance(event, OrderTradeEvent):
            self.orders[(event.symbol, event.order_id)] = event
            if event.execution_type == "TRADE" and event.last_filled_qty > 0:
                if event.last_filled_price is None or event.last_filled_price <= 0:
                    raise LifecycleError("trade event has filled quantity without valid price")
                fill_key = (event.symbol, event.order_id, event.trade_id)
                if fill_key not in self._seen_fill_keys:
                    self._seen_fill_keys.add(fill_key)
                    self.fills.append(
                        FillEvidence(
                            symbol=event.symbol,
                            order_id=event.order_id,
                            client_order_id=event.client_order_id,
                            trade_id=event.trade_id,
                            side=event.side,
                            qty=event.last_filled_qty,
                            price=event.last_filled_price,
                            realized_pnl=event.realized_pnl,
                            commission=event.commission,
                            commission_asset=event.commission_asset,
                            transaction_time_ms=event.transaction_time_ms,
                        )
                    )
            return

        if isinstance(event, AccountUpdateEvent):
            for position in event.positions:
                self.positions[position.symbol] = position.position_amount
            return

        if isinstance(event, ListenKeyExpiredEvent):
            self.stream_valid = False
            return

        if isinstance(event, MarginCallEvent):
            self.margin_call_seen = True
            return

        if isinstance(event, OtherPrivateEvent):
            return

        raise LifecycleError(f"unsupported private event type: {type(event).__name__}")

    def reconcile(
        self,
        snapshot: AuthoritativeLifecycleSnapshot,
    ) -> tuple[ReconciliationIssue, ...]:
        """Compare stream-derived state with REST; any material mismatch blocks execution."""
        issues: list[ReconciliationIssue] = []
        if snapshot.hedged_mode:
            issues.append(
                ReconciliationIssue(
                    severity=ReconciliationSeverity.BLOCK,
                    code="HEDGE_MODE_FORBIDDEN",
                    symbol=None,
                    detail="scanner requires Binance One-way Mode",
                )
            )
        if not self.stream_valid:
            issues.append(
                ReconciliationIssue(
                    severity=ReconciliationSeverity.BLOCK,
                    code="PRIVATE_STREAM_INVALID",
                    symbol=None,
                    detail="private stream expired; REST recovery and reconnect required",
                )
            )
        if self.margin_call_seen:
            issues.append(
                ReconciliationIssue(
                    severity=ReconciliationSeverity.BLOCK,
                    code="MARGIN_CALL_SEEN",
                    symbol=None,
                    detail="margin call event observed; new entries must remain blocked",
                )
            )

        authoritative = {
            position.symbol: (
                position.size if position.side == "Buy" else -position.size
                if position.side == "Sell"
                else Decimal(0)
            )
            for position in snapshot.positions
        }
        for symbol in set(self.positions) | set(authoritative):
            observed = self.positions.get(symbol, Decimal(0))
            actual = authoritative.get(symbol, Decimal(0))
            if observed != actual:
                issues.append(
                    ReconciliationIssue(
                        severity=ReconciliationSeverity.BLOCK,
                        code="POSITION_DRIFT",
                        symbol=symbol,
                        detail=f"stream position={observed} REST position={actual}",
                    )
                )

        if len(snapshot.open_positions) > 3:
            issues.append(
                ReconciliationIssue(
                    severity=ReconciliationSeverity.BLOCK,
                    code="MAX_POSITIONS_BREACHED",
                    symbol=None,
                    detail=f"exchange reports {len(snapshot.open_positions)} open positions",
                )
            )
        for position in snapshot.open_positions:
            if position.leverage is None or position.leverage > Decimal("3"):
                issues.append(
                    ReconciliationIssue(
                        severity=ReconciliationSeverity.BLOCK,
                        code="LEVERAGE_BREACH",
                        symbol=position.symbol,
                        detail=f"exchange leverage={position.leverage}",
                    )
                )
        return tuple(issues)
