from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from crypto_scanner.binance.models import (
    Candle,
    FundingRatePoint,
    InstrumentInfo,
    OpenInterestPoint,
    TickerSnapshot,
    decimal_optional,
    decimal_required,
)
from crypto_scanner.safety import BINANCE_DEMO_REST_URL, assert_binance_demo_url


class BinancePublicApiError(RuntimeError):
    """Raised when Binance Futures Demo returns invalid public market data."""


_INTERVALS = {
    "1": "1m",
    "3": "3m",
    "5": "5m",
    "15": "15m",
    "60": "1h",
    "240": "4h",
}
_INTERVAL_MS = {
    "1": 60_000,
    "3": 3 * 60_000,
    "5": 5 * 60_000,
    "15": 15 * 60_000,
    "60": 60 * 60_000,
    "240": 4 * 60 * 60_000,
}
_OI_PERIODS = {
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


def _parse_klines(rows: list[Any]) -> tuple[Candle, ...]:
    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 8:
            raise BinancePublicApiError("kline row has unexpected shape")
        candles.append(
            Candle(
                start_time_ms=int(row[0]),
                open=decimal_required(row[1], "open"),
                high=decimal_required(row[2], "high"),
                low=decimal_required(row[3], "low"),
                close=decimal_required(row[4], "close"),
                volume=decimal_required(row[5], "volume"),
                turnover=decimal_required(row[7], "quote_volume"),
            )
        )
    return tuple(candles)


class BinanceDemoPublicRestClient:
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
        self._exchange_info_cache: dict[str, Any] | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BinanceDemoPublicRestClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, path: str, params: Mapping[str, object] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        assert_binance_demo_url(url)
        response = self._client.get(url, params=params or {})
        response.raise_for_status()
        payload = response.json()
        is_error_payload = (
            isinstance(payload, dict)
            and isinstance(payload.get("code"), int)
            and payload["code"] < 0
        )
        if is_error_payload:
            raise BinancePublicApiError(
                f"Binance public API error code={payload.get('code')} msg={payload.get('msg')}"
            )
        return payload

    def _exchange_info(self) -> dict[str, Any]:
        if self._exchange_info_cache is None:
            payload = self._get("/fapi/v1/exchangeInfo")
            if not isinstance(payload, dict):
                raise BinancePublicApiError("exchangeInfo response must be a JSON object")
            self._exchange_info_cache = payload
        return self._exchange_info_cache

    def get_instrument(self, symbol: str) -> InstrumentInfo:
        symbol = symbol.upper()
        items = [
            item
            for item in self._exchange_info().get("symbols", [])
            if item.get("symbol") == symbol
        ]
        if len(items) != 1:
            raise BinancePublicApiError(f"expected one instrument for {symbol}, got {len(items)}")
        item = items[0]
        filters = {entry.get("filterType"): entry for entry in item.get("filters", [])}
        price_filter = filters.get("PRICE_FILTER") or {}
        lot_filter = filters.get("LOT_SIZE") or {}
        market_lot = filters.get("MARKET_LOT_SIZE") or {}
        notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
        status = "Trading" if item.get("status") == "TRADING" else str(item.get("status", ""))
        return InstrumentInfo(
            symbol=symbol,
            status=status,
            contract_type=str(item.get("contractType", "")),
            base_coin=str(item.get("baseAsset", "")),
            quote_coin=str(item.get("quoteAsset", "")),
            settle_coin=str(item.get("marginAsset", "")),
            tick_size=decimal_required(price_filter.get("tickSize"), "PRICE_FILTER.tickSize"),
            min_order_qty=decimal_required(lot_filter.get("minQty"), "LOT_SIZE.minQty"),
            qty_step=decimal_required(lot_filter.get("stepSize"), "LOT_SIZE.stepSize"),
            min_notional_value=decimal_optional(
                notional_filter.get("notional") or notional_filter.get("minNotional")
            ),
            max_order_qty=decimal_optional(lot_filter.get("maxQty")),
            max_market_order_qty=decimal_optional(market_lot.get("maxQty")),
            min_leverage=None,
            max_leverage=None,
            leverage_step=None,
        )

    def get_ticker(self, symbol: str) -> TickerSnapshot:
        symbol = symbol.upper()
        stats = self._get("/fapi/v1/ticker/24hr", {"symbol": symbol})
        book = self._get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        premium = self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        oi = self._get("/fapi/v1/openInterest", {"symbol": symbol})
        if not all(isinstance(item, dict) for item in (stats, book, premium, oi)):
            raise BinancePublicApiError("ticker component response must be a JSON object")
        open_interest = decimal_optional(oi.get("openInterest"))
        next_funding = premium.get("nextFundingTime")
        return TickerSnapshot(
            symbol=symbol,
            last_price=decimal_required(stats.get("lastPrice"), "lastPrice"),
            mark_price=decimal_required(premium.get("markPrice"), "markPrice"),
            index_price=decimal_required(premium.get("indexPrice"), "indexPrice"),
            bid_price=decimal_required(book.get("bidPrice"), "bidPrice"),
            ask_price=decimal_required(book.get("askPrice"), "askPrice"),
            bid_size=decimal_optional(book.get("bidQty")),
            ask_size=decimal_optional(book.get("askQty")),
            volume_24h=decimal_optional(stats.get("volume")),
            turnover_24h=decimal_optional(stats.get("quoteVolume")),
            open_interest=open_interest,
            open_interest_value=None,
            funding_rate=decimal_optional(premium.get("lastFundingRate")),
            next_funding_time_ms=int(next_funding) if next_funding not in (None, "") else None,
        )

    def get_klines(self, symbol: str, interval: str, *, limit: int = 200) -> tuple[Candle, ...]:
        if interval not in _INTERVALS:
            raise ValueError(f"unsupported scanner interval: {interval}")
        if not 1 <= limit <= 1500:
            raise ValueError("kline limit must be between 1 and 1500")
        rows = self._get(
            "/fapi/v1/klines",
            {"symbol": symbol.upper(), "interval": _INTERVALS[interval], "limit": limit},
        )
        if not isinstance(rows, list):
            raise BinancePublicApiError("kline response must be a JSON array")
        return _parse_klines(rows)

    def get_klines_window(
        self,
        symbol: str,
        interval: str,
        *,
        start_time_ms: int,
        end_time_ms: int,
        max_candles: int = 50_000,
    ) -> tuple[Candle, ...]:
        """Fetch an exact historical window with bounded, forward-only pagination."""
        if interval not in _INTERVALS:
            raise ValueError(f"unsupported scanner interval: {interval}")
        if start_time_ms < 0 or end_time_ms <= start_time_ms:
            raise ValueError("kline window requires 0 <= start_time_ms < end_time_ms")
        if not 1 <= max_candles <= 50_000:
            raise ValueError("max_candles must be between 1 and 50000")

        interval_ms = _INTERVAL_MS[interval]
        cursor = start_time_ms
        by_start: dict[int, Candle] = {}
        while cursor < end_time_ms:
            remaining = max_candles - len(by_start)
            if remaining <= 0:
                raise BinancePublicApiError("requested kline window exceeds max_candles guard")
            batch_limit = min(1500, remaining)
            rows = self._get(
                "/fapi/v1/klines",
                {
                    "symbol": symbol.upper(),
                    "interval": _INTERVALS[interval],
                    "startTime": cursor,
                    "endTime": end_time_ms - 1,
                    "limit": batch_limit,
                },
            )
            if not isinstance(rows, list):
                raise BinancePublicApiError("kline window response must be a JSON array")
            batch = _parse_klines(rows)
            if not batch:
                break

            for candle in batch:
                if start_time_ms <= candle.start_time_ms < end_time_ms:
                    by_start[candle.start_time_ms] = candle
            last_start = max(candle.start_time_ms for candle in batch)
            next_cursor = last_start + interval_ms
            if next_cursor <= cursor:
                raise BinancePublicApiError("kline pagination did not advance")
            cursor = next_cursor
            if len(batch) < batch_limit:
                break

        if cursor < end_time_ms and len(by_start) >= max_candles:
            raise BinancePublicApiError("requested kline window exceeds max_candles guard")
        return tuple(by_start[key] for key in sorted(by_start))

    def get_open_interest(
        self,
        symbol: str,
        *,
        interval_time: str = "5min",
        limit: int = 50,
    ) -> tuple[OpenInterestPoint, ...]:
        """Return the truthful current OI snapshot available on Futures Testnet.

        Binance's historical `/futures/data/openInterestHist` surface redirects to the
        Demo UI on this test environment. We therefore do not manufacture history.
        A cross-run OI delta can be derived later from durable sampled snapshots.
        """
        if interval_time not in _OI_PERIODS:
            raise ValueError(f"unsupported open interest interval: {interval_time}")
        if not 1 <= limit <= 500:
            raise ValueError("open interest limit must be between 1 and 500")
        symbol = symbol.upper()
        oi = self._get("/fapi/v1/openInterest", {"symbol": symbol})
        server_time = self._get("/fapi/v1/time")
        if not isinstance(oi, dict) or not isinstance(server_time, dict):
            raise BinancePublicApiError("current open-interest response must be JSON objects")
        return (
            OpenInterestPoint(
                timestamp_ms=int(server_time["serverTime"]),
                open_interest=decimal_required(oi.get("openInterest"), "openInterest"),
            ),
        )

    def get_funding_history(self, symbol: str, *, limit: int = 50) -> tuple[FundingRatePoint, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("funding history limit must be between 1 and 1000")
        rows = self._get("/fapi/v1/fundingRate", {"symbol": symbol.upper(), "limit": limit})
        if not isinstance(rows, list):
            raise BinancePublicApiError("funding response must be a JSON array")
        points = [
            FundingRatePoint(
                timestamp_ms=int(item["fundingTime"]),
                funding_rate=decimal_required(item.get("fundingRate"), "fundingRate"),
            )
            for item in rows
        ]
        points.sort(key=lambda point: point.timestamp_ms)
        return tuple(points)
