from __future__ import annotations

from decimal import Decimal

import httpx

from crypto_scanner.bybit.public_rest import BybitPublicRestClient


def _response(request: httpx.Request, result: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        request=request,
        json={"retCode": 0, "retMsg": "OK", "result": result, "time": 1},
    )


def test_instrument_metadata_uses_exchange_precision() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(
            request,
            {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "Trading",
                        "contractType": "LinearPerpetual",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "settleCoin": "USDT",
                        "priceFilter": {"tickSize": "0.10"},
                        "lotSizeFilter": {
                            "minOrderQty": "0.001",
                            "qtyStep": "0.001",
                            "minNotionalValue": "5",
                            "maxOrderQty": "100",
                            "maxMktOrderQty": "50",
                        },
                        "leverageFilter": {
                            "minLeverage": "1",
                            "maxLeverage": "100",
                            "leverageStep": "0.01",
                        },
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = BybitPublicRestClient(client=http_client)
        instrument = client.get_instrument("BTCUSDT")

    assert instrument.symbol == "BTCUSDT"
    assert instrument.tick_size == Decimal("0.10")
    assert instrument.qty_step == Decimal("0.001")
    assert instrument.min_notional_value == Decimal("5")


def test_ticker_exposes_spread_and_crypto_native_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(
            request,
            {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "lastPrice": "100.05",
                        "markPrice": "100.04",
                        "indexPrice": "100.03",
                        "bid1Price": "100.00",
                        "ask1Price": "100.10",
                        "bid1Size": "10",
                        "ask1Size": "9",
                        "volume24h": "5000",
                        "turnover24h": "500000",
                        "openInterest": "1500",
                        "openInterestValue": "150000",
                        "fundingRate": "0.0001",
                        "nextFundingTime": "123456789",
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        ticker = BybitPublicRestClient(client=http_client).get_ticker("BTCUSDT")

    assert ticker.mark_price == Decimal("100.04")
    assert ticker.open_interest == Decimal("1500")
    assert ticker.funding_rate == Decimal("0.0001")
    assert ticker.spread == Decimal("0.10")
    assert ticker.spread_bps > 0


def test_kline_response_is_sorted_oldest_to_newest() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(
            request,
            {
                "list": [
                    ["2000", "2", "3", "1", "2.5", "10", "25"],
                    ["1000", "1", "2", "0.5", "1.5", "8", "12"],
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        candles = BybitPublicRestClient(client=http_client).get_klines(
            "BTCUSDT", "5", limit=2
        )

    assert [candle.start_time_ms for candle in candles] == [1000, 2000]
    assert candles[-1].close == Decimal("2.5")


def test_open_interest_and_funding_are_sorted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("open-interest"):
            return _response(
                request,
                {
                    "list": [
                        {"timestamp": "2000", "openInterest": "12"},
                        {"timestamp": "1000", "openInterest": "10"},
                    ]
                },
            )
        return _response(
            request,
            {
                "list": [
                    {"fundingRateTimestamp": "2000", "fundingRate": "0.0002"},
                    {"fundingRateTimestamp": "1000", "fundingRate": "0.0001"},
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = BybitPublicRestClient(client=http_client)
        oi = client.get_open_interest("BTCUSDT", limit=2)
        funding = client.get_funding_history("BTCUSDT", limit=2)

    assert [point.timestamp_ms for point in oi] == [1000, 2000]
    assert [point.timestamp_ms for point in funding] == [1000, 2000]
