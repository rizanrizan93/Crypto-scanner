from __future__ import annotations

# Temporary compatibility boundary while exchange-neutral models are extracted in the next phase.
# These immutable Decimal-based structures are exchange-agnostic despite their historical module.
from crypto_scanner.bybit.models import (
    Candle,
    FundingRatePoint,
    InstrumentInfo,
    OpenInterestPoint,
    TickerSnapshot,
    decimal_optional,
    decimal_required,
)
from crypto_scanner.bybit.private_models import (
    OrderSnapshot,
    PositionSnapshot,
    WalletCoin,
    WalletSnapshot,
)

__all__ = [
    "Candle",
    "FundingRatePoint",
    "InstrumentInfo",
    "OpenInterestPoint",
    "OrderSnapshot",
    "PositionSnapshot",
    "TickerSnapshot",
    "WalletCoin",
    "WalletSnapshot",
    "decimal_optional",
    "decimal_required",
]
