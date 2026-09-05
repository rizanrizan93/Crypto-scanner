from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from crypto_scanner.bybit.models import (
    Candle,
    FundingRatePoint,
    InstrumentInfo,
    OpenInterestPoint,
    TickerSnapshot,
    decimal_optional,
    decimal_required,
)
from crypto_scanner.safety import BYBIT_TESTNET_REST_URL, assert_testnet_url


class BybitPublicApiError(RuntimeError):
    """Raised when Bybit returns an unsuccessful public API response."""


class BybitAccessForbiddenError(BybitPublicApiError):
    """Raised when Bybit rejects the source with HTTP 403.

    Bybit documents multiple possible 403 causes, including source-IP restrictions and IP-level
    request controls. Callers must treat this as an environment/access condition, not infer a single
    cause from the status code alone.
    """


class BybitPublicRestClient:
    def __init__(
        self,
        base_url: str = BYBIT_TESTNET_REST_URL,
        *,
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = assert_testnet_url(base_url).rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BybitPublicRestClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, path: str, params: Mapping[str, object]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        assert_testnet_url(url)
        response = self._client.get(url, params=params)
        if response.status_code == 403:
            raise BybitAccessForbiddenError(
                "Bybit Testnet returned HTTP 403. Possible causes include a restricted source IP "
                "or IP-level request controls; venue connectivity must be verified from the "
                "intended permitted runtime host."
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise BybitPublicApiError("Bybit response must be a JSON object")
        if payload.get("retCode") != 0:
            raise BybitPublicApiError(
                f"Bybit public API error retCode={payload.get('retCode')} "
                f"retMsg={payload.get('retMsg')}"
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise BybitPublicApiError("Bybit response is missing result object")
        return result

    def get_instrument(self, symbol: str) -> InstrumentInfo:
        result = self._get(
            "/v5/market/instruments-info",
            {"category": "linear", "symbol": symbol.upper()},
        )
        items = result.get("list") or []
        if len(items) != 1:
            raise BybitPublicApiError(f"expected one instrument for {symbol}, got {len(items)}")
        item = items[0]
        price_filter = item.get("priceFilter") or {}
        lot_filter = item.get("lotSizeFilter") or {}
        leverage_filter = item.get("leverageFilter") or {}
        return InstrumentInfo(
            symbol=item["symbol"],
            status=item["status"],
            contract_type=item.get("contractType", ""),
            base_coin=item.get("baseCoin", ""),
            quote_coin=item.get("quoteCoin", ""),
            settle_coin=item.get("settleCoin", ""),
            tick_size=decimal_required(price_filter.get("tickSize"), "tickSize"),
            min_order_qty=decimal_required(lot_filter.get("minOrderQty"), "minOrderQty"),
            qty_step=decimal_required(lot_filter.get("qtyStep"), "qtyStep"),
            min_notional_value=decimal_optional(lot_filter.get("minNotionalValue")),
            max_order_qty=decimal_optional(lot_filter.get("maxOrderQty")),
            max_market_order_qty=decimal_optional(lot_filter.get("maxMktOrderQty")),
            min_leverage=decimal_optional(leverage_filter.get("minLeverage")),
            max_leverage=decimal_optional(leverage_filter.get("maxLeverage")),
            leverage_step=decimal_optional(leverage_filter.get("leverageStep")),
        )

    def get_ticker(self, symbol: str) -> TickerSnapshot:
        result = self._get(
            "/v5/market/tickers",
            {"category": "linear", "symbol": symbol.upper()},
        )
        items = result.get("list") or []
        if len(items) != 1:
            raise BybitPublicApiError(f"expected one ticker for {symbol}, got {len(items)}")
        item = items[0]
        next_funding = item.get("nextFundingTime")
        return TickerSnapshot(
            symbol=item["symbol"],
            last_price=decimal_required(item.get("lastPrice"), "lastPrice"),
            mark_price=decimal_required(item.get("markPrice"), "markPrice"),
            index_price=decimal_required(item.get("indexPrice"), "indexPrice"),
            bid_price=decimal_required(item.get("bid1Price"), "bid1Price"),
            ask_price=decimal_required(item.get("ask1Price"), "ask1Price"),
            bid_size=decimal_optional(item.get("bid1Size")),
            ask_size=decimal_optional(item.get("ask1Size")),
            volume_24h=decimal_optional(item.get("volume24h")),
            turnover_24h=decimal_optional(item.get("turnover24h")),
            open_interest=decimal_optional(item.get("openInterest")),
            open_interest_value=decimal_optional(item.get("openInterestValue")),
            funding_rate=decimal_optional(item.get("fundingRate")),
            next_funding_time_ms=int(next_funding) if next_funding not in (None, "") else None,
        )

    def get_klines(self, symbol: str, interval: str, *, limit: int = 200) -> tuple[Candle, ...]:
        if interval not in {"1", "3", "5", "15", "60", "240"}:
            raise ValueError(f"unsupported scanner interval: {interval}")
        if not 1 <= limit <= 1000:
            raise ValueError("kline limit must be between 1 and 1000")
        result = self._get(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol.upper(),
                "interval": interval,
                "limit": limit,
            },
        )
        rows = result.get("list") or []
        candles = [
            Candle(
                start_time_ms=int(row[0]),
                open=decimal_required(row[1], "open"),
                high=decimal_required(row[2], "high"),
                low=decimal_required(row[3], "low"),
                close=decimal_required(row[4], "close"),
                volume=decimal_required(row[5], "volume"),
                turnover=decimal_required(row[6], "turnover"),
            )
            for row in rows
        ]
        candles.sort(key=lambda candle: candle.start_time_ms)
        return tuple(candles)

    def get_open_interest(
        self,
        symbol: str,
        *,
        interval_time: str = "5min",
        limit: int = 50,
    ) -> tuple[OpenInterestPoint, ...]:
        if interval_time not in {"5min", "15min", "30min", "1h", "4h", "1d"}:
            raise ValueError(f"unsupported open interest interval: {interval_time}")
        if not 1 <= limit <= 200:
            raise ValueError("open interest limit must be between 1 and 200")
        result = self._get(
            "/v5/market/open-interest",
            {
                "category": "linear",
                "symbol": symbol.upper(),
                "intervalTime": interval_time,
                "limit": limit,
            },
        )
        points = [
            OpenInterestPoint(
                timestamp_ms=int(item["timestamp"]),
                open_interest=decimal_required(item.get("openInterest"), "openInterest"),
            )
            for item in result.get("list") or []
        ]
        points.sort(key=lambda point: point.timestamp_ms)
        return tuple(points)

    def get_funding_history(
        self,
        symbol: str,
        *,
        limit: int = 50,
    ) -> tuple[FundingRatePoint, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("funding history limit must be between 1 and 200")
        result = self._get(
            "/v5/market/funding/history",
            {"category": "linear", "symbol": symbol.upper(), "limit": limit},
        )
        points = [
            FundingRatePoint(
                timestamp_ms=int(item["fundingRateTimestamp"]),
                funding_rate=decimal_required(item.get("fundingRate"), "fundingRate"),
            )
            for item in result.get("list") or []
        ]
        points.sort(key=lambda point: point.timestamp_ms)
        return tuple(points)
