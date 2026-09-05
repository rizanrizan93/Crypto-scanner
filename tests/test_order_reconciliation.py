from __future__ import annotations

import httpx

from crypto_scanner.bybit.auth import BybitTestnetCredentials
from crypto_scanner.bybit.private_rest import BybitPrivateReadOnlyClient

TIMESTAMP = 1_800_000_000_000
CREDENTIALS = BybitTestnetCredentials(api_key="key", api_secret="secret")
ORDER_LINK_ID = "cs-btcusdt-0123456789abcdef"


def _response(request: httpx.Request, rows: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(
        200,
        request=request,
        json={
            "retCode": 0,
            "retMsg": "OK",
            "result": {"list": rows, "nextPageCursor": ""},
            "time": TIMESTAMP,
        },
    )


def _order(status: str) -> dict[str, object]:
    return {
        "orderId": "exchange-order-1",
        "orderLinkId": ORDER_LINK_ID,
        "symbol": "BTCUSDT",
        "side": "Buy",
        "orderStatus": status,
        "orderType": "Market",
        "timeInForce": "IOC",
        "price": "0",
        "qty": "0.01",
        "avgPrice": "100",
        "leavesQty": "0",
        "cumExecQty": "0.01",
        "cumExecValue": "1",
        "cumExecFee": "0.0005",
        "triggerPrice": "",
        "takeProfit": "105",
        "stopLoss": "98",
        "reduceOnly": False,
        "closeOnTrigger": False,
        "createdTime": str(TIMESTAMP),
        "updatedTime": str(TIMESTAMP + 1),
    }


def test_realtime_order_is_authoritative_when_found() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _response(request, [_order("Filled")])

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with http_client:
        client = BybitPrivateReadOnlyClient(
            CREDENTIALS,
            client=http_client,
            time_source_ms=lambda: TIMESTAMP,
        )
        order = client.get_order_by_link_id(
            symbol="BTCUSDT",
            order_link_id=ORDER_LINK_ID,
        )

    assert order is not None
    assert order.order_status == "Filled"
    assert calls == ["/v5/order/realtime"]


def test_missing_realtime_order_falls_back_to_history() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v5/order/realtime":
            return _response(request, [])
        return _response(request, [_order("Cancelled")])

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with http_client:
        client = BybitPrivateReadOnlyClient(
            CREDENTIALS,
            client=http_client,
            time_source_ms=lambda: TIMESTAMP,
        )
        order = client.get_order_by_link_id(
            symbol="BTCUSDT",
            order_link_id=ORDER_LINK_ID,
        )

    assert order is not None
    assert order.order_status == "Cancelled"
    assert calls == ["/v5/order/realtime", "/v5/order/history"]


def test_missing_order_in_both_sources_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(request, [])

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with http_client:
        client = BybitPrivateReadOnlyClient(
            CREDENTIALS,
            client=http_client,
            time_source_ms=lambda: TIMESTAMP,
        )
        order = client.get_order_by_link_id(
            symbol="BTCUSDT",
            order_link_id=ORDER_LINK_ID,
        )

    assert order is None
