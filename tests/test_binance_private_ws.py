from decimal import Decimal

import httpx
import pytest

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_ws import (
    AccountUpdateEvent,
    BinanceDemoListenKeyClient,
    OrderTradeEvent,
    parse_private_event,
)
from crypto_scanner.safety import SafetyError, assert_binance_demo_ws_url


def test_order_trade_update_parses_fill_evidence() -> None:
    event = parse_private_event(
        {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1001,
            "T": 1000,
            "o": {
                "s": "XRPUSDT",
                "c": "cs-xrp-1",
                "i": 123,
                "S": "BUY",
                "ps": "BOTH",
                "o": "MARKET",
                "x": "TRADE",
                "X": "FILLED",
                "q": "3.6",
                "z": "3.6",
                "l": "3.6",
                "L": "1.4150",
                "ap": "1.4150",
                "rp": "0",
                "n": "0.002",
                "N": "USDT",
                "t": 77,
                "R": False,
            },
        }
    )
    assert isinstance(event, OrderTradeEvent)
    assert event.last_filled_qty == Decimal("3.6")
    assert event.last_filled_price == Decimal("1.4150")
    assert event.trade_id == "77"


def test_account_update_parses_signed_position() -> None:
    event = parse_private_event(
        {
            "e": "ACCOUNT_UPDATE",
            "E": 2001,
            "T": 2000,
            "a": {
                "m": "ORDER",
                "B": [{"a": "USDT", "wb": "5000", "cw": "4990", "bc": "0"}],
                "P": [
                    {
                        "s": "XRPUSDT",
                        "pa": "3.6",
                        "ep": "1.415",
                        "bep": "1.416",
                        "cr": "0",
                        "up": "0.01",
                        "mt": "cross",
                        "iw": "0",
                        "ps": "BOTH",
                    }
                ],
            },
        }
    )
    assert isinstance(event, AccountUpdateEvent)
    assert event.positions[0].position_amount == Decimal("3.6")
    assert event.reason == "ORDER"


def test_live_binance_ws_is_hard_rejected() -> None:
    with pytest.raises(SafetyError):
        assert_binance_demo_ws_url("wss://fstream.binance.com/ws/secret")


def test_listen_key_start_uses_api_key_only() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"listenKey": "demo-listen-key"})

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    credentials = BinanceDemoCredentials(api_key="key", api_secret="secret")
    client = BinanceDemoListenKeyClient(credentials, client=http)
    assert client.start() == "demo-listen-key"
    assert requests[0].method == "POST"
    assert requests[0].headers["X-MBX-APIKEY"] == "key"
