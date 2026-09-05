import hashlib
import hmac

import pytest

from crypto_scanner.binance.auth import (
    BinanceDemoCredentials,
    BinanceDemoCredentialsError,
    encode_query,
    sign_query,
)


def test_credentials_repr_does_not_expose_secrets() -> None:
    credentials = BinanceDemoCredentials("public-key", "private-secret")
    rendered = repr(credentials)
    assert "public-key" not in rendered
    assert "private-secret" not in rendered


def test_credentials_fail_closed_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BINANCE_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_DEMO_API_SECRET", raising=False)
    with pytest.raises(BinanceDemoCredentialsError):
        BinanceDemoCredentials.from_environment()


def test_query_signing_uses_exact_transmitted_query_string() -> None:
    query = encode_query({"timestamp": 123456789, "recvWindow": 5000, "symbol": "BTCUSDT"})
    expected = hmac.new(b"secret", query.encode(), hashlib.sha256).hexdigest()
    assert sign_query("secret", query) == expected
    assert query == "recvWindow=5000&symbol=BTCUSDT&timestamp=123456789"
