from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, AsyncIterator

import httpx
import websockets

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.models import decimal_optional, decimal_required
from crypto_scanner.safety import (
    BINANCE_DEMO_REST_URL,
    BINANCE_DEMO_WS_STREAM_URL,
    assert_binance_demo_url,
    assert_binance_demo_ws_url,
)


class BinancePrivateStreamError(RuntimeError):
    """Raised when the Binance Futures Demo private stream cannot be used safely."""


@dataclass(frozen=True, slots=True)
class OrderTradeEvent:
    event_time_ms: int
    transaction_time_ms: int
    symbol: str
    client_order_id: str
    order_id: str
    side: str
    position_side: str
    order_type: str
    execution_type: str
    order_status: str
    original_qty: Decimal
    cumulative_qty: Decimal
    last_filled_qty: Decimal
    last_filled_price: Decimal | None
    average_price: Decimal | None
    realized_pnl: Decimal
    commission: Decimal | None
    commission_asset: str | None
    trade_id: str
    reduce_only: bool


@dataclass(frozen=True, slots=True)
class BalanceUpdate:
    asset: str
    wallet_balance: Decimal
    cross_wallet_balance: Decimal | None
    balance_change: Decimal | None


@dataclass(frozen=True, slots=True)
class PositionUpdate:
    symbol: str
    position_side: str
    position_amount: Decimal
    entry_price: Decimal | None
    break_even_price: Decimal | None
    accumulated_realized: Decimal | None
    unrealized_pnl: Decimal | None
    margin_type: str
    isolated_wallet: Decimal | None


@dataclass(frozen=True, slots=True)
class AccountUpdateEvent:
    event_time_ms: int
    transaction_time_ms: int
    reason: str
    balances: tuple[BalanceUpdate, ...]
    positions: tuple[PositionUpdate, ...]


@dataclass(frozen=True, slots=True)
class ListenKeyExpiredEvent:
    event_time_ms: int


@dataclass(frozen=True, slots=True)
class MarginCallEvent:
    event_time_ms: int
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OtherPrivateEvent:
    event_type: str
    event_time_ms: int | None
    raw: dict[str, Any]


PrivateEvent = (
    OrderTradeEvent
    | AccountUpdateEvent
    | ListenKeyExpiredEvent
    | MarginCallEvent
    | OtherPrivateEvent
)


def _int_required(value: object, field: str) -> int:
    if value in (None, ""):
        raise BinancePrivateStreamError(f"missing required integer field: {field}")
    return int(value)


def parse_private_event(payload: dict[str, Any]) -> PrivateEvent:
    event_type = str(payload.get("e") or "")
    if event_type == "ORDER_TRADE_UPDATE":
        order = payload.get("o")
        if not isinstance(order, dict):
            raise BinancePrivateStreamError("ORDER_TRADE_UPDATE missing order payload")
        return OrderTradeEvent(
            event_time_ms=_int_required(payload.get("E"), "event.E"),
            transaction_time_ms=_int_required(payload.get("T"), "event.T"),
            symbol=str(order.get("s") or ""),
            client_order_id=str(order.get("c") or ""),
            order_id=str(order.get("i") or ""),
            side=str(order.get("S") or ""),
            position_side=str(order.get("ps") or "BOTH"),
            order_type=str(order.get("o") or ""),
            execution_type=str(order.get("x") or ""),
            order_status=str(order.get("X") or ""),
            original_qty=decimal_required(order.get("q"), "order.q"),
            cumulative_qty=decimal_required(order.get("z"), "order.z"),
            last_filled_qty=decimal_required(order.get("l"), "order.l"),
            last_filled_price=decimal_optional(order.get("L")),
            average_price=decimal_optional(order.get("ap")),
            realized_pnl=decimal_optional(order.get("rp")) or Decimal(0),
            commission=decimal_optional(order.get("n")),
            commission_asset=(
                str(order.get("N")) if order.get("N") not in (None, "") else None
            ),
            trade_id=str(order.get("t") or ""),
            reduce_only=bool(order.get("R", False)),
        )

    if event_type == "ACCOUNT_UPDATE":
        account = payload.get("a")
        if not isinstance(account, dict):
            raise BinancePrivateStreamError("ACCOUNT_UPDATE missing account payload")
        balances: list[BalanceUpdate] = []
        for item in account.get("B") or []:
            if not isinstance(item, dict):
                continue
            balances.append(
                BalanceUpdate(
                    asset=str(item.get("a") or ""),
                    wallet_balance=decimal_required(item.get("wb"), "balance.wb"),
                    cross_wallet_balance=decimal_optional(item.get("cw")),
                    balance_change=decimal_optional(item.get("bc")),
                )
            )
        positions: list[PositionUpdate] = []
        for item in account.get("P") or []:
            if not isinstance(item, dict):
                continue
            positions.append(
                PositionUpdate(
                    symbol=str(item.get("s") or ""),
                    position_side=str(item.get("ps") or "BOTH"),
                    position_amount=decimal_required(item.get("pa"), "position.pa"),
                    entry_price=decimal_optional(item.get("ep")),
                    break_even_price=decimal_optional(item.get("bep")),
                    accumulated_realized=decimal_optional(item.get("cr")),
                    unrealized_pnl=decimal_optional(item.get("up")),
                    margin_type=str(item.get("mt") or ""),
                    isolated_wallet=decimal_optional(item.get("iw")),
                )
            )
        return AccountUpdateEvent(
            event_time_ms=_int_required(payload.get("E"), "event.E"),
            transaction_time_ms=_int_required(payload.get("T"), "event.T"),
            reason=str(account.get("m") or ""),
            balances=tuple(balances),
            positions=tuple(positions),
        )

    if event_type == "listenKeyExpired":
        return ListenKeyExpiredEvent(event_time_ms=_int_required(payload.get("E"), "event.E"))
    if event_type == "MARGIN_CALL":
        return MarginCallEvent(
            event_time_ms=_int_required(payload.get("E"), "event.E"),
            raw=payload,
        )
    event_time = payload.get("E")
    return OtherPrivateEvent(
        event_type=event_type,
        event_time_ms=int(event_time) if event_time not in (None, "") else None,
        raw=payload,
    )


class BinanceDemoListenKeyClient:
    """Create and keep alive a Futures Demo user-data stream without order writes."""

    def __init__(
        self,
        credentials: BinanceDemoCredentials,
        *,
        base_url: str = BINANCE_DEMO_REST_URL,
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        credentials.validate()
        self.credentials = credentials
        self.base_url = assert_binance_demo_url(base_url).rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BinanceDemoListenKeyClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str) -> dict[str, Any]:
        url = f"{self.base_url}/fapi/v1/listenKey"
        assert_binance_demo_url(url)
        response = self._client.request(
            method,
            url,
            headers={"X-MBX-APIKEY": self.credentials.api_key},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise BinancePrivateStreamError("listenKey response is not JSON") from exc
        if response.is_error:
            raise BinancePrivateStreamError(
                f"listenKey request failed status={response.status_code} payload={payload}"
            )
        if not isinstance(payload, dict):
            raise BinancePrivateStreamError("listenKey response must be a JSON object")
        return payload

    def start(self) -> str:
        payload = self._request("POST")
        listen_key = str(payload.get("listenKey") or "")
        if not listen_key:
            raise BinancePrivateStreamError("listenKey response did not contain a key")
        return listen_key

    def keepalive(self) -> None:
        self._request("PUT")

    def stop(self) -> None:
        self._request("DELETE")


class BinanceDemoPrivateStream:
    """Async Futures Demo user stream; REST must reconcile state after reconnect."""

    def __init__(
        self,
        credentials: BinanceDemoCredentials,
        *,
        ws_base_url: str = BINANCE_DEMO_WS_STREAM_URL,
        keepalive_seconds: float = 45 * 60,
    ) -> None:
        credentials.validate()
        if keepalive_seconds <= 0:
            raise ValueError("keepalive_seconds must be positive")
        self.credentials = credentials
        self.ws_base_url = assert_binance_demo_ws_url(ws_base_url).rstrip("/")
        self.keepalive_seconds = keepalive_seconds

    async def events(self) -> AsyncIterator[PrivateEvent]:
        with BinanceDemoListenKeyClient(self.credentials) as listen:
            listen_key = listen.start()
            ws_url = assert_binance_demo_ws_url(f"{self.ws_base_url}/ws/{listen_key}")
            stop = asyncio.Event()

            async def keepalive_loop() -> None:
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=self.keepalive_seconds)
                    except TimeoutError:
                        await asyncio.to_thread(listen.keepalive)

            keepalive_task = asyncio.create_task(keepalive_loop())
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=150,
                    ping_timeout=600,
                    close_timeout=5,
                    max_size=2**20,
                ) as socket:
                    async for raw in socket:
                        payload = json.loads(raw)
                        if not isinstance(payload, dict):
                            raise BinancePrivateStreamError(
                                "private websocket payload must be a JSON object"
                            )
                        event = parse_private_event(payload)
                        yield event
                        if isinstance(event, ListenKeyExpiredEvent):
                            raise BinancePrivateStreamError(
                                "listenKey expired; REST reconciliation required before reconnect"
                            )
            finally:
                stop.set()
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass
                try:
                    await asyncio.to_thread(listen.stop)
                except BinancePrivateStreamError:
                    pass
