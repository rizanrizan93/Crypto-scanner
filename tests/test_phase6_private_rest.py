from decimal import Decimal

import httpx

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient


def test_open_algo_orders_parse_new_order_type_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/openAlgoOrders"
        return httpx.Response(
            200,
            json=[
                {
                    "algoId": 1,
                    "clientAlgoId": "cs-sl-1",
                    "symbol": "XRPUSDT",
                    "side": "SELL",
                    "orderType": "STOP_MARKET",
                    "algoStatus": "NEW",
                    "triggerPrice": "1.4008",
                    "quantity": "3.6",
                    "reduceOnly": True,
                    "updateTime": 1234,
                }
            ],
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = BinanceDemoPrivateReadOnlyClient(
        BinanceDemoCredentials(api_key="key", api_secret="secret"),
        client=http,
        time_source_ms=lambda: 1000,
    )
    orders = client.get_open_algo_orders("XRPUSDT")
    assert orders[0].order_type == "STOP_MARKET"
    assert orders[0].quantity == Decimal("3.6")
    assert orders[0].reduce_only is True


def test_user_trades_and_income_are_exact_decimal_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/userTrades":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "XRPUSDT",
                        "id": 77,
                        "orderId": 123,
                        "side": "BUY",
                        "positionSide": "BOTH",
                        "price": "1.4150",
                        "qty": "3.6",
                        "quoteQty": "5.094",
                        "realizedPnl": "0",
                        "commission": "0.0020376",
                        "commissionAsset": "USDT",
                        "buyer": True,
                        "maker": False,
                        "time": 1234,
                    }
                ],
            )
        if request.url.path == "/fapi/v1/income":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "XRPUSDT",
                        "incomeType": "FUNDING_FEE",
                        "income": "-0.001",
                        "asset": "USDT",
                        "time": 1500,
                        "tranId": 9,
                        "tradeId": "",
                        "info": "FUNDING_FEE",
                    }
                ],
            )
        raise AssertionError(request.url.path)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = BinanceDemoPrivateReadOnlyClient(
        BinanceDemoCredentials(api_key="key", api_secret="secret"),
        client=http,
        time_source_ms=lambda: 1000,
    )
    fill = client.get_user_trades("XRPUSDT")[0]
    income = client.get_income_history(symbol="XRPUSDT")[0]
    assert fill.price == Decimal("1.4150")
    assert fill.commission == Decimal("0.0020376")
    assert income.income == Decimal("-0.001")
