from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_write import (
    BinanceTestnetOrderClient,
    SubmissionState,
    UnknownSubmissionOutcome,
    build_protection_plan,
)
from crypto_scanner.execution_plan import EntryOrderPlan, ExecutionPlanError, TestnetExecutionArm

TIMESTAMP = 1_800_000_000_000
CREDENTIALS = BinanceDemoCredentials(api_key="key", api_secret="secret")


def _plan() -> EntryOrderPlan:
    return EntryOrderPlan(
        signal_id="signal-1",
        order_link_id="cs-btcusdt-0123456789abcdef",
        symbol="BTCUSDT",
        side="Buy",
        qty=Decimal("0.010"),
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        take_profit_1=Decimal("103"),
        take_profit_2=Decimal("105"),
        risk_fraction=Decimal("0.005"),
        risk_amount=Decimal("0.02"),
        notional=Decimal("1"),
        leverage_equivalent=Decimal("0.001"),
    )


def _assert_signature(request: httpx.Request) -> dict[str, list[str]]:
    body = request.content.decode()
    unsigned, signature = body.rsplit("&signature=", 1)
    expected = hmac.new(b"secret", unsigned.encode(), hashlib.sha256).hexdigest()
    assert signature == expected
    assert request.headers["X-MBX-APIKEY"] == "key"
    return parse_qs(unsigned)


def test_disarmed_entry_fails_before_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with http_client:
        client = BinanceTestnetOrderClient(
            CREDENTIALS,
            TestnetExecutionArm(False),
            client=http_client,
            time_source_ms=lambda: TIMESTAMP,
        )
        with pytest.raises(ExecutionPlanError, match="disarmed"):
            client.submit_entry(_plan())
    assert calls == 0


def test_entry_is_signed_market_ack_and_never_assumed_filled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/order"
        params = _assert_signature(request)
        assert params["symbol"] == ["BTCUSDT"]
        assert params["side"] == ["BUY"]
        assert params["type"] == ["MARKET"]
        assert params["positionSide"] == ["BOTH"]
        assert params["quantity"] == ["0.010"]
        assert params["newClientOrderId"] == [_plan().order_link_id]
        assert params["newOrderRespType"] == ["ACK"]
        return httpx.Response(
            200,
            request=request,
            json={
                "orderId": 123,
                "clientOrderId": _plan().order_link_id,
                "transactTime": TIMESTAMP + 1,
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with http_client:
        client = BinanceTestnetOrderClient(
            CREDENTIALS,
            TestnetExecutionArm(True),
            client=http_client,
            time_source_ms=lambda: TIMESTAMP,
        )
        ack = client.submit_entry(_plan())
    assert ack.order_id == "123"
    assert ack.client_order_id == _plan().order_link_id
    assert ack.state is SubmissionState.PENDING_RECONCILIATION


def test_transport_failure_is_unknown_and_never_blindly_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("network outcome unknown", request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with http_client:
        client = BinanceTestnetOrderClient(
            CREDENTIALS,
            TestnetExecutionArm(True),
            client=http_client,
            time_source_ms=lambda: TIMESTAMP,
        )
        with pytest.raises(UnknownSubmissionOutcome) as error:
            client.submit_entry(_plan())
    assert calls == 1
    assert error.value.client_id == _plan().order_link_id


def test_server_side_protection_uses_current_algo_order_api() -> None:
    seen: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/algoOrder"
        params = _assert_signature(request)
        seen.append(params)
        client_id = params["clientAlgoId"][0]
        return httpx.Response(
            200,
            request=request,
            json={"algoId": len(seen), "clientAlgoId": client_id, "createTime": TIMESTAMP},
        )

    protection = build_protection_plan(_plan(), Decimal("0.010"))
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with http_client:
        client = BinanceTestnetOrderClient(
            CREDENTIALS,
            TestnetExecutionArm(True),
            client=http_client,
            time_source_ms=lambda: TIMESTAMP,
        )
        stop_ack = client.submit_stop_loss(protection)
        tp_ack = client.submit_take_profit(protection)

    assert [item["type"][0] for item in seen] == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    for params in seen:
        assert params["algoType"] == ["CONDITIONAL"]
        assert params["positionSide"] == ["BOTH"]
        assert params["side"] == ["SELL"]
        assert params["quantity"] == ["0.010"]
        assert params["workingType"] == ["MARK_PRICE"]
        assert params["reduceOnly"] == ["true"]
        assert params["newOrderRespType"] == ["ACK"]
    assert seen[0]["triggerPrice"] == ["98"]
    assert seen[1]["triggerPrice"] == ["105"]
    assert stop_ack.state is SubmissionState.PENDING_RECONCILIATION
    assert tp_ack.state is SubmissionState.PENDING_RECONCILIATION
    assert protection.stop_client_algo_id != protection.take_profit_client_algo_id
