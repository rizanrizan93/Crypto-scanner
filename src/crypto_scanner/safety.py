from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse


class SafetyError(RuntimeError):
    """Raised when runtime configuration violates the hard safety contract."""


class Venue(StrEnum):
    BYBIT = "BYBIT"
    BINANCE = "BINANCE"


class ExecutionEnvironment(StrEnum):
    TESTNET = "TESTNET"


BYBIT_TESTNET_REST_URL = "https://api-testnet.bybit.com"
BYBIT_TESTNET_PUBLIC_WS_URL = "wss://stream-testnet.bybit.com/v5/public/linear"
BYBIT_TESTNET_PRIVATE_WS_URL = "wss://stream-testnet.bybit.com/v5/private"
# Binance UI calls this environment Futures Demo Trading. The official Binance
# tooling still exposes the USDⓈ-M API test base at testnet.binancefuture.com.
BINANCE_DEMO_REST_URL = "https://testnet.binancefuture.com"
BINANCE_DEMO_WS_STREAM_URL = "wss://stream.binancefuture.com"

_BYBIT_FORBIDDEN_HOSTS = {
    "api.bybit.com",
    "api.bytick.com",
    "stream.bybit.com",
}
_BINANCE_FORBIDDEN_HOSTS = {
    "api.binance.com",
    "fapi.binance.com",
    "fstream.binance.com",
    "ws-fapi.binance.com",
}


@dataclass(frozen=True, slots=True)
class SafetyContract:
    venue: Venue = Venue.BINANCE
    environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET
    live_trading_locked: bool = True
    max_risk_per_trade: float = 0.01
    # This is the portfolio-wide logical risk-slot cap. Every new stack layer consumes
    # one slot even though Binance One-way Mode nets same-symbol layers into one position.
    max_concurrent_positions: int = 10
    max_layers_per_symbol: int = 3
    profitable_stacking_enabled: bool = True
    # Retained for constructor/backward compatibility. The production contract now
    # permits same-symbol exposure only through the bounded profitable-stacking gates.
    one_position_per_symbol: bool = False
    max_leverage: float = 3.0

    def validate(self) -> None:
        if self.venue is not Venue.BINANCE:
            raise SafetyError("active Crypto Scanner venue is Binance Futures Demo only")
        if self.environment is not ExecutionEnvironment.TESTNET:
            raise SafetyError(
                "LIVE is hard locked; non-live execution is the only allowed environment"
            )
        if self.live_trading_locked is not True:
            raise SafetyError("live_trading_locked must remain true")
        if not 0 < self.max_risk_per_trade <= 0.01:
            raise SafetyError("max_risk_per_trade must be > 0 and <= 1%")
        if not 1 <= self.max_concurrent_positions <= 10:
            raise SafetyError("max_concurrent_positions must be between 1 and 10 risk slots")
        if not 1 <= self.max_layers_per_symbol <= 3:
            raise SafetyError("max_layers_per_symbol must be between 1 and 3")
        if self.profitable_stacking_enabled:
            if self.one_position_per_symbol:
                raise SafetyError(
                    "one_position_per_symbol cannot be true while profitable stacking is enabled"
                )
        elif not self.one_position_per_symbol:
            raise SafetyError(
                "same-symbol exposure requires profitable_stacking_enabled=true"
            )
        if not 1 <= self.max_leverage <= 3:
            raise SafetyError("max_leverage must be bounded between 1x and 3x during testing")


def assert_testnet_url(url: str) -> str:
    """Legacy Bybit Testnet endpoint guard retained until the Bybit adapter is removed."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in _BYBIT_FORBIDDEN_HOSTS:
        raise SafetyError(f"production Bybit endpoint forbidden: {host}")
    if host not in {"api-testnet.bybit.com", "stream-testnet.bybit.com"}:
        raise SafetyError(
            f"unapproved Bybit Testnet endpoint forbidden: {host or '<missing-host>'}"
        )
    if parsed.scheme not in {"https", "wss"}:
        raise SafetyError(f"insecure or unsupported scheme forbidden: {parsed.scheme}")
    return url


def assert_binance_demo_url(url: str) -> str:
    """Allow only the Binance USDⓈ-M Futures test REST host; reject LIVE hosts."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in _BINANCE_FORBIDDEN_HOSTS:
        raise SafetyError(f"production Binance endpoint forbidden: {host}")
    if host != "testnet.binancefuture.com":
        raise SafetyError(
            f"unapproved Binance Futures test endpoint forbidden: {host or '<missing-host>'}"
        )
    if parsed.scheme != "https":
        raise SafetyError(f"insecure or unsupported Binance test scheme forbidden: {parsed.scheme}")
    return url


def assert_binance_demo_ws_url(url: str) -> str:
    """Allow only the Binance USDⓈ-M Futures test WebSocket stream host."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in _BINANCE_FORBIDDEN_HOSTS:
        raise SafetyError(f"production Binance websocket endpoint forbidden: {host}")
    if host != "stream.binancefuture.com":
        raise SafetyError(
            f"unapproved Binance Futures test websocket forbidden: {host or '<missing-host>'}"
        )
    if parsed.scheme != "wss":
        raise SafetyError(f"insecure or unsupported Binance websocket scheme: {parsed.scheme}")
    return url
