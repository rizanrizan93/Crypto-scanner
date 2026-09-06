from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from crypto_scanner.binance.models import decimal_required
from crypto_scanner.safety import BINANCE_DEMO_REST_URL, assert_binance_demo_url


class BinanceMicrostructureError(RuntimeError):
    """Raised when Binance Demo microstructure evidence is invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class BinanceMicrostructureEvidence:
    symbol: str
    orderbook_imbalance: Decimal
    taker_pressure: Decimal
    observed_at_ms: int
    depth_levels: int
    trade_count: int


class BinanceDemoMicrostructureClient:
    def __init__(
        self,
        base_url: str = BINANCE_DEMO_REST_URL,
        *,
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = assert_binance_demo_url(base_url).rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BinanceDemoMicrostructureClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, object]) -> Any:
        url = f"{self.base_url}{path}"
        assert_binance_demo_url(url)
        try:
            response = self._client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BinanceMicrostructureError(
                f"Binance microstructure request failed path={path}: {exc}"
            ) from exc
        if isinstance(payload, dict) and isinstance(payload.get("code"), int):
            if payload["code"] < 0:
                raise BinanceMicrostructureError(
                    f"Binance microstructure API error code={payload.get('code')} "
                    f"msg={payload.get('msg')}"
                )
        return payload

    def get_evidence(
        self,
        symbol: str,
        *,
        depth_limit: int = 20,
        trade_limit: int = 100,
    ) -> BinanceMicrostructureEvidence:
        if depth_limit not in {5, 10, 20, 50, 100, 500, 1000}:
            raise ValueError("unsupported Binance Futures depth limit")
        if not 1 <= trade_limit <= 1000:
            raise ValueError("trade_limit must be between 1 and 1000")
        symbol = symbol.upper()
        depth = self._get("/fapi/v1/depth", {"symbol": symbol, "limit": depth_limit})
        trades = self._get("/fapi/v1/trades", {"symbol": symbol, "limit": trade_limit})
        if not isinstance(depth, dict):
            raise BinanceMicrostructureError("depth response must be an object")
        if not isinstance(trades, list):
            raise BinanceMicrostructureError("recent-trades response must be an array")

        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
            raise BinanceMicrostructureError("orderbook depth is missing bid/ask levels")
        bid_qty = sum(
            (
                decimal_required(level[1], "depth.bid.qty")
                for level in bids
                if isinstance(level, list) and len(level) >= 2
            ),
            Decimal(0),
        )
        ask_qty = sum(
            (
                decimal_required(level[1], "depth.ask.qty")
                for level in asks
                if isinstance(level, list) and len(level) >= 2
            ),
            Decimal(0),
        )
        book_total = bid_qty + ask_qty
        if book_total <= 0:
            raise BinanceMicrostructureError("orderbook depth has zero aggregate quantity")
        orderbook_imbalance = (bid_qty - ask_qty) / book_total

        taker_buy_qty = Decimal(0)
        taker_sell_qty = Decimal(0)
        valid_trade_count = 0
        latest_trade_ms = 0
        for item in trades:
            if not isinstance(item, dict):
                continue
            qty = decimal_required(item.get("qty"), "trade.qty")
            if qty <= 0:
                continue
            valid_trade_count += 1
            latest_trade_ms = max(latest_trade_ms, int(item.get("time") or 0))
            if bool(item.get("isBuyerMaker", False)):
                taker_sell_qty += qty
            else:
                taker_buy_qty += qty
        trade_total = taker_buy_qty + taker_sell_qty
        if valid_trade_count == 0 or trade_total <= 0:
            raise BinanceMicrostructureError("recent trades contain no usable taker volume")
        taker_pressure = (taker_buy_qty - taker_sell_qty) / trade_total

        depth_time = int(depth.get("T") or depth.get("E") or 0)
        observed_at_ms = max(depth_time, latest_trade_ms, time.time_ns() // 1_000_000)
        return BinanceMicrostructureEvidence(
            symbol=symbol,
            orderbook_imbalance=orderbook_imbalance,
            taker_pressure=taker_pressure,
            observed_at_ms=observed_at_ms,
            depth_levels=min(len(bids), len(asks)),
            trade_count=valid_trade_count,
        )
