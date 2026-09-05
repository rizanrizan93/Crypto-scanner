from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from urllib.parse import urlencode


class BybitCredentialsError(RuntimeError):
    """Raised when required Bybit Testnet credentials are missing or invalid."""


@dataclass(frozen=True, slots=True)
class BybitTestnetCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def validate(self) -> None:
        if not self.api_key.strip():
            raise BybitCredentialsError("BYBIT_TESTNET_API_KEY is empty")
        if not self.api_secret.strip():
            raise BybitCredentialsError("BYBIT_TESTNET_API_SECRET is empty")

    @classmethod
    def from_environment(cls) -> BybitTestnetCredentials:
        api_key = os.getenv("BYBIT_TESTNET_API_KEY", "")
        api_secret = os.getenv("BYBIT_TESTNET_API_SECRET", "")
        credentials = cls(api_key=api_key, api_secret=api_secret)
        credentials.validate()
        return credentials


def encode_query(params: dict[str, object]) -> str:
    """Create one deterministic query string for both signing and transmission."""
    pairs: list[tuple[str, str]] = []
    for key in sorted(params):
        value = params[key]
        if value is None:
            continue
        encoded_value = ("true" if value else "false") if isinstance(value, bool) else str(value)
        pairs.append((key, encoded_value))
    return urlencode(pairs)


def sign_get_request(
    *,
    api_key: str,
    api_secret: str,
    timestamp_ms: int,
    recv_window_ms: int,
    query_string: str,
) -> str:
    if timestamp_ms <= 0:
        raise ValueError("timestamp_ms must be positive")
    if not 1 <= recv_window_ms <= 5000:
        raise ValueError("recv_window_ms must be between 1 and 5000")
    payload = f"{timestamp_ms}{api_key}{recv_window_ms}{query_string}"
    return hmac.new(
        api_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
