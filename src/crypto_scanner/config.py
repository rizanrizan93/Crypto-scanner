from __future__ import annotations

import os
from dataclasses import dataclass, field

from crypto_scanner.safety import (
    BINANCE_DEMO_REST_URL,
    SafetyContract,
    assert_binance_demo_url,
)

DEFAULT_UNIVERSE = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "TRXUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "SUIUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "DOTUSDT",
    "UNIUSDT",
    "AAVEUSDT",
    "NEARUSDT",
    "ETCUSDT",
    "FILUSDT",
    "ATOMUSDT",
)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    safety: SafetyContract = field(default_factory=SafetyContract)
    universe: tuple[str, ...] = DEFAULT_UNIVERSE
    binance_rest_url: str = BINANCE_DEMO_REST_URL
    supabase_enabled: bool = False

    def validate(self) -> None:
        self.safety.validate()
        if not self.universe:
            raise ValueError("universe cannot be empty")
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("universe contains duplicate symbols")
        if any(not symbol.endswith("USDT") for symbol in self.universe):
            raise ValueError("initial universe is restricted to USDT instruments")
        assert_binance_demo_url(self.binance_rest_url)


def load_runtime_config() -> RuntimeConfig:
    """Load non-sensitive Binance Futures Demo runtime configuration."""
    universe_env = os.getenv("CRYPTO_SCANNER_UNIVERSE", "")
    universe = (
        tuple(item.strip().upper() for item in universe_env.split(",") if item.strip())
        if universe_env
        else DEFAULT_UNIVERSE
    )
    config = RuntimeConfig(universe=universe)
    config.validate()
    return config
