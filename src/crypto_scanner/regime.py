from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from crypto_scanner.bybit.models import Candle
from crypto_scanner.structure import StructuralBias, StructureState
from crypto_scanner.technical import adx, atr, atr_expansion_ratio, ema, momentum


class MarketRegime(StrEnum):
    TREND = "TREND"
    RANGE = "RANGE"
    EXPANSION = "EXPANSION"
    CHAOTIC = "HIGH_VOLATILITY_CHAOTIC"


@dataclass(frozen=True, slots=True)
class RegimeState:
    regime: MarketRegime
    adx14: Decimal
    atr14: Decimal
    atr_pct: Decimal
    atr_expansion: Decimal
    ema20: Decimal
    ema50: Decimal
    momentum10: Decimal


def classify_regime(candles: tuple[Candle, ...], structure: StructureState) -> RegimeState:
    closes = tuple(candle.close for candle in candles)
    last_close = closes[-1]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    atr14 = atr(candles, 14)
    atr_pct = atr14 / last_close
    expansion = atr_expansion_ratio(candles, 14)
    adx14 = adx(candles, 14)
    momentum10 = momentum(closes, 10)

    ema_bullish = last_close > ema20 > ema50
    ema_bearish = last_close < ema20 < ema50
    structure_aligned = (
        structure.bias is StructuralBias.BULLISH and ema_bullish
    ) or (
        structure.bias is StructuralBias.BEARISH and ema_bearish
    )

    if expansion >= Decimal("2.0"):
        regime = MarketRegime.CHAOTIC
    elif expansion >= Decimal("1.35") and abs(momentum10) >= atr_pct * Decimal("1.5"):
        regime = MarketRegime.EXPANSION
    elif structure_aligned and adx14 >= Decimal("20"):
        regime = MarketRegime.TREND
    else:
        regime = MarketRegime.RANGE

    return RegimeState(
        regime=regime,
        adx14=adx14,
        atr14=atr14,
        atr_pct=atr_pct,
        atr_expansion=expansion,
        ema20=ema20,
        ema50=ema50,
        momentum10=momentum10,
    )
