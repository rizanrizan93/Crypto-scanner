from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterable
from typing import Any

from websockets.asyncio.client import connect

from crypto_scanner.bybit.models import (
    OrderBookLevel,
    OrderBookUpdate,
    PublicTrade,
    decimal_required,
)
from crypto_scanner.safety import BYBIT_TESTNET_PUBLIC_WS_URL, assert_testnet_url


class BybitPublicWebSocketError(RuntimeError):
    """Raised when a public WebSocket message violates the expected contract."""


def build_public_topics(symbols: Iterable[str], *, orderbook_depth: int = 50) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
    if not normalized:
        raise ValueError("at least one symbol is required")
    if orderbook_depth not in {1, 50, 200, 1000}:
        raise ValueError("unsupported Bybit linear orderbook depth")

    topics: list[str] = []
    for symbol in normalized:
        if not symbol.endswith("USDT"):
            raise ValueError(f"Phase 1 public stream only accepts USDT symbols: {symbol}")
        topics.extend(
            (
                f"tickers.{symbol}",
                f"publicTrade.{symbol}",
                f"orderbook.{orderbook_depth}.{symbol}",
            )
        )
    return tuple(topics)


def parse_public_trades(message: dict[str, Any]) -> tuple[PublicTrade, ...]:
    topic = str(message.get("topic", ""))
    if not topic.startswith("publicTrade."):
        return ()

    trades: list[PublicTrade] = []
    for item in message.get("data") or []:
        trades.append(
            PublicTrade(
                symbol=str(item["s"]),
                timestamp_ms=int(item["T"]),
                side=str(item["S"]),
                price=decimal_required(item.get("p"), "trade.price"),
                size=decimal_required(item.get("v"), "trade.size"),
                trade_id=str(item["i"]),
            )
        )
    return tuple(trades)


def parse_orderbook_update(message: dict[str, Any]) -> OrderBookUpdate | None:
    topic = str(message.get("topic", ""))
    if not topic.startswith("orderbook."):
        return None

    data = message.get("data")
    if not isinstance(data, dict):
        raise BybitPublicWebSocketError("orderbook message missing data object")

    def parse_levels(rows: object, side: str) -> tuple[OrderBookLevel, ...]:
        if not isinstance(rows, list):
            return ()
        return tuple(
            OrderBookLevel(
                price=decimal_required(row[0], f"{side}.price"),
                size=decimal_required(row[1], f"{side}.size"),
            )
            for row in rows
        )

    update_id_raw = data.get("u")
    if update_id_raw is None:
        raise BybitPublicWebSocketError("orderbook message missing update id")
    sequence_raw = data.get("seq")
    engine_timestamp_raw = message.get("cts")
    return OrderBookUpdate(
        symbol=str(data["s"]),
        update_type=str(message.get("type", "")),
        timestamp_ms=int(message.get("ts", 0)),
        engine_timestamp_ms=(
            int(engine_timestamp_raw) if engine_timestamp_raw is not None else None
        ),
        update_id=int(update_id_raw),
        sequence=int(sequence_raw) if sequence_raw is not None else None,
        bids=parse_levels(data.get("b"), "bid"),
        asks=parse_levels(data.get("a"), "ask"),
    )


class BybitPublicWebSocket:
    def __init__(
        self,
        symbols: Iterable[str],
        *,
        url: str = BYBIT_TESTNET_PUBLIC_WS_URL,
        orderbook_depth: int = 50,
    ) -> None:
        self.url = assert_testnet_url(url)
        self.topics = build_public_topics(symbols, orderbook_depth=orderbook_depth)

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded public messages and reconnect with bounded backoff on disconnects."""
        delay_seconds = 1.0
        while True:
            try:
                async with connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=4096,
                ) as websocket:
                    await websocket.send(json.dumps({"op": "subscribe", "args": self.topics}))
                    delay_seconds = 1.0
                    async for raw_message in websocket:
                        if not isinstance(raw_message, str):
                            continue
                        message = json.loads(raw_message)
                        if not isinstance(message, dict):
                            continue
                        if message.get("success") is False:
                            raise BybitPublicWebSocketError(
                                f"subscription rejected: {message.get('ret_msg') or message}"
                            )
                        yield message
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 30.0)
