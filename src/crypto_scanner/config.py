from __future__ import annotations

import os
from dataclasses import dataclass, field

from crypto_scanner.safety import (
    BYBIT_TESTNET_PRIVATE_WS_URL,
    BYBIT_TESTNET_PUBLIC_WS_URL,
    BYBIT_TESTNET_REST_URL,
    SafetyContract,
    assert_testnet_url,
)

DEFAULT_UNIVERSE = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    safety: SafetyContract = field(default_factory=SafetyContract)
    universe: tuple[str, ...] = DEFAULT_UNIVERSE
    bybit_rest_url: str = BYBIT_TESTNET_REST_URL
    bybit_public_ws_url: str = BYBIT_TESTNET_PUBLIC_WS_URL
    bybit_private_ws_url: str = BYBIT_TESTNET_PRIVATE_WS_URL
    supabase_enabled: bool = False

    def validate(self) -> None:
        self.safety.validate()
        if not self.universe:
            raise ValueError("universe cannot be empty")
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("universe contains duplicate symbols")
        if any(not symbol.endswith("USDT") for symbol in self.universe):
            raise ValueError("Phase 0 universe is restricted to USDT instruments")
        assert_testnet_url(self.bybit_rest_url)
        assert_testnet_url(self.bybit_public_ws_url)
        assert_testnet_url(self.bybit_private_ws_url)


def load_runtime_config() -> RuntimeConfig:
    """Load only non-sensitive Phase 0 configuration.

    Production endpoint overrides are deliberately unsupported. Supabase defaults off until the
    dedicated Crypto Scanner project is created.
    """
    universe_env = os.getenv("CRYPTO_SCANNER_UNIVERSE", "")
    universe = (
        tuple(item.strip().upper() for item in universe_env.split(",") if item.strip())
        if universe_env
        else DEFAULT_UNIVERSE
    )
    config = RuntimeConfig(universe=universe)
    config.validate()
    return config
