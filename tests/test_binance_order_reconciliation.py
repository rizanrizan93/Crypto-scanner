from __future__ import annotations

import hashlib
import hmac

import httpx

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient

TIMESTAMP = 1_800_000_000_000
CREDENTIALS = BinanceDemoCredentials(api_key="key", api_secret="secret")


def _assert_query_signature(request: httpx.Request) -> None:
    query = request.url.query.decode()
    unsigned, signature = query.rsplit("&signature=", 1)
    expected = hmac.new(b"secret", unsigned.encode(), hashlib.sha256).hexdigest()
    assert signature == expected
    assert request.headers["X-MBX-APIKEY"] == "key"


def test_entry_reconciliation_queries_by_deterministic_client_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/order"
        _assert_query_signature(request)
        assert request.url.params["symbol"] == "BTCUSDT"
        assert request.url.params["origClientOrderId"] == "cs-btcusdt-abc"
        return httpx.Response(
            200,
            request=request,
            json={
                "orderId": 123,
                "clientOrderId": "cs-btcusdt-abc",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "status": "FILLED",
                "type": "MARKET",
                "timeInForce": "GTC",
                "price": "0",
                "origQty": "0.010",
                "executedQty": "0.010",
                "avgPrice": "100",
                "cumQuote": "1",
                "reduceOnly": False,
                "closePosition": False,
                "time": TIMESTAMP,
                "updateTime": TIMESTAMP + 1,
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with http_client:
        client = BinanceDemoPrivateReadOnlyClient(
            CREDENTIALS,
            client=http_client,
            time_source_ms=lambda: TIMESTAMP,
        )
        order = client.get_order_by_client_id("BTCUSDT", "cs-btcusdt-abc")
    assert order.order_status == "FILLED"
    assert str(order.cum_exec_qty) == "0.010"
    assert str(order.avg_price) == "100"


def test_algo_reconciliation_queries_by_client_algo_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/algoOrder"
        _assert_query_signature(request)
        assert request.url.params["clientAlgoId"] == "cs-sl-abc"
        return httpx.Response(
            200,
            request=request,
            json={
                "algoId": 456,
                "clientAlgoId": "cs-sl-abc",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "algoStatus": "NEW",
                "triggerPrice": "98",
                "quantity": "0.010",
                "reduceOnly": True,
                "updateTime": TIMESTAMP + 1,
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with http_client:
        client = BinanceDemoPrivateReadOnlyClient(
            CREDENTIALS,
            client=http_client,
            time_source_ms=lambda: TIMESTAMP,
        )
        algo = client.get_algo_order_by_client_id("cs-sl-abc")
    assert algo.algo_id == "456"
    assert algo.status == "NEW"
    assert str(algo.trigger_price) == "98"
    assert algo.reduce_only is True
