from __future__ import annotations

import hashlib
import hmac

import pytest

from crypto_scanner.bybit.auth import (
    BybitCredentialsError,
    BybitTestnetCredentials,
    encode_query,
    sign_get_request,
)


def test_query_encoding_is_deterministic() -> None:
    query = encode_query(
        {
            "settleCoin": "USDT",
            "category": "linear",
            "limit": 50,
            "cursor": None,
            "openOnly": 0,
        }
    )
    assert query == "category=linear&limit=50&openOnly=0&settleCoin=USDT"


def test_get_signature_matches_bybit_hmac_contract() -> None:
    timestamp = 1_700_000_000_000
    api_key = "test-key"
    api_secret = "test-secret"
    recv_window = 5000
    query = "accountType=UNIFIED&coin=USDT"

    expected_payload = f"{timestamp}{api_key}{recv_window}{query}"
    expected = hmac.new(
        api_secret.encode(),
        expected_payload.encode(),
        hashlib.sha256,
    ).hexdigest()

    actual = sign_get_request(
        api_key=api_key,
        api_secret=api_secret,
        timestamp_ms=timestamp,
        recv_window_ms=recv_window,
        query_string=query,
    )
    assert actual == expected


def test_credentials_repr_does_not_expose_secrets() -> None:
    credentials = BybitTestnetCredentials(
        api_key="visible-key-must-not-leak",
        api_secret="visible-secret-must-not-leak",
    )
    rendered = repr(credentials)
    assert "visible-key-must-not-leak" not in rendered
    assert "visible-secret-must-not-leak" not in rendered


def test_credentials_fail_closed_when_environment_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BYBIT_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_TESTNET_API_SECRET", raising=False)
    with pytest.raises(BybitCredentialsError):
        BybitTestnetCredentials.from_environment()


def test_credentials_load_only_testnet_secret_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BYBIT_TESTNET_API_KEY", "key")
    monkeypatch.setenv("BYBIT_TESTNET_API_SECRET", "secret")
    credentials = BybitTestnetCredentials.from_environment()
    assert credentials.api_key == "key"
    assert credentials.api_secret == "secret"
