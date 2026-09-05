from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from urllib.parse import urlencode


class BinanceDemoCredentialsError(RuntimeError):
    """Raised when Binance Futures Demo credentials are missing or invalid."""


@dataclass(frozen=True, slots=True)
class BinanceDemoCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def validate(self) -> None:
        if not self.api_key.strip():
            raise BinanceDemoCredentialsError("BINANCE_DEMO_API_KEY is empty")
        if not self.api_secret.strip():
            raise BinanceDemoCredentialsError("BINANCE_DEMO_API_SECRET is empty")

    @classmethod
    def from_environment(cls) -> BinanceDemoCredentials:
        credentials = cls(
            api_key=os.getenv("BINANCE_DEMO_API_KEY", ""),
            api_secret=os.getenv("BINANCE_DEMO_API_SECRET", ""),
        )
        credentials.validate()
        return credentials


def encode_query(params: dict[str, object]) -> str:
    pairs: list[tuple[str, str]] = []
    for key in sorted(params):
        value = params[key]
        if value is None:
            continue
        encoded = ("true" if value else "false") if isinstance(value, bool) else str(value)
        pairs.append((key, encoded))
    return urlencode(pairs)


def sign_query(api_secret: str, query_string: str) -> str:
    if not api_secret:
        raise BinanceDemoCredentialsError("API secret cannot be empty")
    return hmac.new(
        api_secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
