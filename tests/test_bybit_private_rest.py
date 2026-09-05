from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal

import httpx
import pytest

from crypto_scanner.bybit.auth import BybitTestnetCredentials
from crypto_scanner.bybit.private_rest import (
    BybitPrivateApiError,
    BybitPrivateReadOnlyClient,
    BybitReadOnlyViolation,
)

TIMESTAMP = 1_700_000_000_000
CREDENTIALS = BybitTestnetCredentials(api_key="key", api_secret="secret")


def _success(request: httpx.Request, result: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        request=request,
        json={"retCode": 0, "retMsg": "OK", "result": result, "time": TIMESTAMP},
    )


def _client(handler: httpx.MockTransport) -> tuple[httpx.Client, BybitPrivateReadOnlyClient]:
    http_client = httpx.Client(transport=handler)
    client = BybitPrivateReadOnlyClient(
        CREDENTIALS,
        client=http_client,
        time_source_ms=lambda: TIMESTAMP,
    )
    return http_client, client


def test_wallet_request_uses_exact_signature() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.query.decode()
        assert query == "accountType=UNIFIED&coin=USDT"
        payload = f"{TIMESTAMP}key5000{query}"
        expected = hmac.new(b"secret", payload.encode(), hashlib.sha256).hexdigest()
        assert request.headers["X-BAPI-SIGN"] == expected
        assert request.headers["X-BAPI-TIMESTAMP"] == str(TIMESTAMP)
        return _success(
            request,
            {
                "list": [
                    {
                        "accountType": "UNIFIED",
                        "totalEquity": "1000.5",
                        "totalWalletBalance": "990",
                        "totalMarginBalance": "995",
                        "totalAvailableBalance": "900",
                        "totalPerpUPL": "5.5",
                        "coin": [
                            {
                                "coin": "USDT",
                                "equity": "1000.5",
                                "walletBalance": "990",
                                "usdValue": "1000.5",
                            }
                        ],
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    http_client, client = _client(transport)
    with http_client:
        wallet = client.get_wallet_balance()

    assert wallet.account_type == "UNIFIED"
    assert wallet.total_equity == Decimal("1000.5")
    assert wallet.total_available_balance == Decimal("900")


def test_non_allowlisted_private_path_fails_before_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _success(request, {})

    transport = httpx.MockTransport(handler)
    http_client, client = _client(transport)
    with http_client:
        with pytest.raises(BybitReadOnlyViolation):
            client._signed_get("/v5/private/not-allowed", {})
    assert calls == 0


def test_repeated_cursor_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _success(request, {"list": [], "nextPageCursor": "same"})

    transport = httpx.MockTransport(handler)
    http_client, client = _client(transport)
    with http_client:
        with pytest.raises(BybitPrivateApiError, match="cursor repeated"):
            client.get_open_orders()
