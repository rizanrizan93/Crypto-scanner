from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import pytest

from crypto_scanner.bybit.auth import BybitTestnetCredentials
from crypto_scanner.bybit.private_write import (
    BybitTestnetOrderClient,
    SubmissionState,
    UnknownSubmissionOutcome,
)
from crypto_scanner.execution_plan import EntryOrderPlan, ExecutionPlanError, TestnetExecutionArm

TIMESTAMP = 1_800_000_000_000
CREDENTIALS = BybitTestnetCredentials(api_key="key", api_secret="secret")


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


def test_disarmed_client_fails_before_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with http_client:
        client = BybitTestnetOrderClient(
            CREDENTIALS,
            TestnetExecutionArm(False),
            client=http_client,
            time_source_ms=lambda: TIMESTAMP,
        )
        with pytest.raises(ExecutionPlanError, match="disarmed"):
            client.submit_entry(_plan())

    assert calls == 0


def test_submit_signs_exact_body_and_returns_pending_reconciliation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        decoded = json.loads(body)
        assert decoded["category"] == "linear"
        assert decoded["orderType"] == "Market"
        assert decoded["tpslMode"] == "Full"
        assert decoded["takeProfit"] == "105"
        assert decoded["stopLoss"] == "98"
        payload = f"{TIMESTAMP}key5000{body}"
        expected = hmac.new(b"secret", payload.encode(), hashlib.sha256).hexdigest()
        assert request.headers["X-BAPI-SIGN"] == expected
        return httpx.Response(
            200,
            request=request,
            json={
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "orderId": "exchange-order-1",
                    "orderLinkId": _plan().order_link_id,
                },
                "time": TIMESTAMP + 1,
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with http_client:
        client = BybitTestnetOrderClient(
            CREDENTIALS,
            TestnetExecutionArm(True),
            client=http_client,
            time_source_ms=lambda: TIMESTAMP,
        )
        ack = client.submit_entry(_plan())

    assert ack.order_id == "exchange-order-1"
    assert ack.order_link_id == _plan().order_link_id
    assert ack.state is SubmissionState.PENDING_RECONCILIATION


def test_transport_failure_is_unknown_and_never_blindly_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("network outcome unknown", request=request)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    with http_client:
        client = BybitTestnetOrderClient(
            CREDENTIALS,
            TestnetExecutionArm(True),
            client=http_client,
            time_source_ms=lambda: TIMESTAMP,
        )
        with pytest.raises(UnknownSubmissionOutcome) as error:
            client.submit_entry(_plan())

    assert calls == 1
    assert error.value.order_link_id == _plan().order_link_id
