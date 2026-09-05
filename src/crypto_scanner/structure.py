from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from crypto_scanner.bybit.models import Candle
from crypto_scanner.technical import TechnicalDataError


class StructuralBias(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    MIXED = "MIXED"


class StructureEvent(StrEnum):
    NONE = "NONE"
    BOS_BULLISH = "BOS_BULLISH"
    BOS_BEARISH = "BOS_BEARISH"
    CHOCH_BULLISH = "CHOCH_BULLISH"
    CHOCH_BEARISH = "CHOCH_BEARISH"


@dataclass(frozen=True, slots=True)
class SwingPoint:
    index: int
    timestamp_ms: int
    price: Decimal


@dataclass(frozen=True, slots=True)
class StructureState:
    bias: StructuralBias
    event: StructureEvent
    recent_highs: tuple[SwingPoint, ...]
    recent_lows: tuple[SwingPoint, ...]
    last_swing_high: Decimal
    last_swing_low: Decimal

    @property
    def directional(self) -> bool:
        return self.bias in {StructuralBias.BULLISH, StructuralBias.BEARISH}


def _swing_highs(candles: tuple[Candle, ...], window: int) -> tuple[SwingPoint, ...]:
    points: list[SwingPoint] = []
    for index in range(window, len(candles) - window):
        candidate = candles[index]
        neighbors = candles[index - window : index] + candles[index + 1 : index + window + 1]
        if all(candidate.high > other.high for other in neighbors):
            points.append(SwingPoint(index, candidate.start_time_ms, candidate.high))
    return tuple(points)


def _swing_lows(candles: tuple[Candle, ...], window: int) -> tuple[SwingPoint, ...]:
    points: list[SwingPoint] = []
    for index in range(window, len(candles) - window):
        candidate = candles[index]
        neighbors = candles[index - window : index] + candles[index + 1 : index + window + 1]
        if all(candidate.low < other.low for other in neighbors):
            points.append(SwingPoint(index, candidate.start_time_ms, candidate.low))
    return tuple(points)


def analyze_structure(candles: tuple[Candle, ...], *, swing_window: int = 2) -> StructureState:
    if swing_window < 1:
        raise ValueError("swing_window must be at least 1")
    if len(candles) < swing_window * 2 + 10:
        raise TechnicalDataError("insufficient candles for market structure")

    highs = _swing_highs(candles, swing_window)
    lows = _swing_lows(candles, swing_window)
    if len(highs) < 2 or len(lows) < 2:
        raise TechnicalDataError("insufficient confirmed swing highs/lows")

    recent_highs = highs[-3:]
    recent_lows = lows[-3:]
    previous_high, last_high = highs[-2], highs[-1]
    previous_low, last_low = lows[-2], lows[-1]

    higher_high = last_high.price > previous_high.price
    higher_low = last_low.price > previous_low.price
    lower_high = last_high.price < previous_high.price
    lower_low = last_low.price < previous_low.price

    if higher_high and higher_low:
        bias = StructuralBias.BULLISH
    elif lower_high and lower_low:
        bias = StructuralBias.BEARISH
    else:
        bias = StructuralBias.MIXED

    last_close = candles[-1].close
    if last_close > last_high.price:
        event = (
            StructureEvent.CHOCH_BULLISH
            if bias is StructuralBias.BEARISH
            else StructureEvent.BOS_BULLISH
        )
    elif last_close < last_low.price:
        event = (
            StructureEvent.CHOCH_BEARISH
            if bias is StructuralBias.BULLISH
            else StructureEvent.BOS_BEARISH
        )
    else:
        event = StructureEvent.NONE

    return StructureState(
        bias=bias,
        event=event,
        recent_highs=recent_highs,
        recent_lows=recent_lows,
        last_swing_high=last_high.price,
        last_swing_low=last_low.price,
    )
