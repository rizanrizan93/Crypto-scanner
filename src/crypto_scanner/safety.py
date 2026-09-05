from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse


class SafetyError(RuntimeError):
    """Raised when runtime configuration violates the hard safety contract."""


class Venue(StrEnum):
    BYBIT = "BYBIT"


class ExecutionEnvironment(StrEnum):
    TESTNET = "TESTNET"


BYBIT_TESTNET_REST_URL = "https://api-testnet.bybit.com"
BYBIT_TESTNET_PUBLIC_WS_URL = "wss://stream-testnet.bybit.com/v5/public/linear"
BYBIT_TESTNET_PRIVATE_WS_URL = "wss://stream-testnet.bybit.com/v5/private"

_FORBIDDEN_HOSTS = {
    "api.bybit.com",
    "api.bytick.com",
    "stream.bybit.com",
}


@dataclass(frozen=True, slots=True)
class SafetyContract:
    venue: Venue = Venue.BYBIT
    environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET
    live_trading_locked: bool = True
    max_risk_per_trade: float = 0.01
    max_concurrent_positions: int = 3
    one_position_per_symbol: bool = True
    max_leverage: float = 3.0

    def validate(self) -> None:
        if self.venue is not Venue.BYBIT:
            raise SafetyError("Phase 0 supports Bybit only")
        if self.environment is not ExecutionEnvironment.TESTNET:
            raise SafetyError("LIVE is hard locked; TESTNET is the only allowed environment")
        if self.live_trading_locked is not True:
            raise SafetyError("live_trading_locked must remain true")
        if not 0 < self.max_risk_per_trade <= 0.01:
            raise SafetyError("max_risk_per_trade must be > 0 and <= 1%")
        if not 1 <= self.max_concurrent_positions <= 3:
            raise SafetyError("max_concurrent_positions must be between 1 and 3")
        if self.one_position_per_symbol is not True:
            raise SafetyError("one-position-per-symbol is mandatory")
        if not 1 <= self.max_leverage <= 3:
            raise SafetyError("max_leverage must be bounded between 1x and 3x during testing")


def assert_testnet_url(url: str) -> str:
    """Reject any URL that is not an explicit Bybit Testnet endpoint."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if host in _FORBIDDEN_HOSTS:
        raise SafetyError(f"production Bybit endpoint forbidden: {host}")
    if host not in {"api-testnet.bybit.com", "stream-testnet.bybit.com"}:
        raise SafetyError(f"unapproved endpoint forbidden: {host or '<missing-host>'}")
    if parsed.scheme not in {"https", "wss"}:
        raise SafetyError(f"insecure or unsupported scheme forbidden: {parsed.scheme}")
    return url
