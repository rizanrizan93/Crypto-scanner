from __future__ import annotations

import httpx
import pytest

from crypto_scanner.binance.public_rest import (
    BinanceDemoPublicRestClient,
    BinancePublicApiError,
)

_MINUTE_MS = 60_000


def _kline_row(start_ms: int) -> list[object]:
    return [
        start_ms,
        "1.0",
        "1.2",
        "0.8",
        "1.1",
        "10",
        start_ms + _MINUTE_MS - 1,
        "11",
        1,
        "5",
        "5.5",
        "0",
    ]


def test_kline_window_paginates_forward_without_duplicates() -> None:
    calls: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        start_ms = int(params["startTime"])
        end_ms = int(params["endTime"])
        limit = int(params["limit"])
        calls.append((start_ms, limit))
        rows: list[list[object]] = []
        cursor = start_ms
        while cursor <= end_ms and len(rows) < limit:
            rows.append(_kline_row(cursor))
            cursor += _MINUTE_MS
        return httpx.Response(200, json=rows)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        client = BinanceDemoPublicRestClient(client=http_client)
        candles = client.get_klines_window(
            "XRPUSDT",
            "1",
            start_time_ms=0,
            end_time_ms=1501 * _MINUTE_MS,
            max_candles=2000,
        )

    assert len(candles) == 1501
    assert candles[0].start_time_ms == 0
    assert candles[-1].start_time_ms == 1500 * _MINUTE_MS
    assert calls == [(0, 1500), (1500 * _MINUTE_MS, 500)]


def test_kline_window_fails_when_guard_is_exhausted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        start_ms = int(params["startTime"])
        end_ms = int(params["endTime"])
        limit = int(params["limit"])
        rows: list[list[object]] = []
        cursor = start_ms
        while cursor <= end_ms and len(rows) < limit:
            rows.append(_kline_row(cursor))
            cursor += _MINUTE_MS
        return httpx.Response(200, json=rows)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        client = BinanceDemoPublicRestClient(client=http_client)
        with pytest.raises(BinancePublicApiError, match="max_candles"):
            client.get_klines_window(
                "XRPUSDT",
                "1",
                start_time_ms=0,
                end_time_ms=2001 * _MINUTE_MS,
                max_candles=2000,
            )
