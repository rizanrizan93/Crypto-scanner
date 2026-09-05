from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_scanner.bybit.models import Candle
from crypto_scanner.regime import MarketRegime, classify_regime
from crypto_scanner.structure import StructuralBias, analyze_structure
from crypto_scanner.technical import TechnicalDataError, closed_candles, ema, rsi, validate_candles


def _wave_candles(*, bullish: bool, count: int = 140, start_ms: int = 0) -> tuple[Candle, ...]:
    pattern = (Decimal("0"), Decimal("1"), Decimal("2"), Decimal("1"), Decimal("0"), Decimal("-1"))
    candles: list[Candle] = []
    for index in range(count):
        trend = Decimal(index) * Decimal("0.35")
        center = Decimal("100") + (trend if bullish else -trend)
        wave = pattern[index % len(pattern)]
        close = center + (wave if bullish else -wave)
        open_price = close - (Decimal("0.15") if bullish else Decimal("-0.15"))
        high = max(open_price, close) + Decimal("0.35")
        low = min(open_price, close) - Decimal("0.35")
        candles.append(
            Candle(
                start_time_ms=start_ms + index * 300_000,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=Decimal("100") + Decimal(index),
                turnover=(Decimal("100") + Decimal(index)) * close,
            )
        )
    return tuple(candles)


def test_closed_candles_excludes_unfinished_bar() -> None:
    candles = _wave_candles(bullish=True, count=3, start_ms=0)
    closed = closed_candles(candles, interval_minutes=5, now_ms=700_000)
    assert len(closed) == 2
    assert closed[-1].start_time_ms == 300_000


def test_validate_candles_rejects_duplicate_timestamp() -> None:
    candles = list(_wave_candles(bullish=True, count=60))
    candles[10] = Candle(
        start_time_ms=candles[9].start_time_ms,
        open=candles[10].open,
        high=candles[10].high,
        low=candles[10].low,
        close=candles[10].close,
        volume=candles[10].volume,
        turnover=candles[10].turnover,
    )
    with pytest.raises(TechnicalDataError, match="strictly increasing"):
        validate_candles(tuple(candles))


def test_ema_and_rsi_are_directionally_sensible() -> None:
    closes = tuple(Decimal(index) for index in range(1, 101))
    assert ema(closes, 20) > ema(closes, 50)
    assert rsi(closes, 14) == Decimal(100)


def test_bullish_wave_produces_bullish_structure() -> None:
    candles = _wave_candles(bullish=True)
    structure = analyze_structure(candles)
    assert structure.bias is StructuralBias.BULLISH
    assert structure.last_swing_high > structure.last_swing_low


def test_bearish_wave_produces_bearish_structure() -> None:
    candles = _wave_candles(bullish=False)
    structure = analyze_structure(candles)
    assert structure.bias is StructuralBias.BEARISH


def test_trending_wave_is_not_classified_chaotic() -> None:
    candles = _wave_candles(bullish=True)
    structure = analyze_structure(candles)
    regime = classify_regime(candles, structure)
    assert regime.regime in {MarketRegime.TREND, MarketRegime.RANGE}
    assert regime.atr14 > 0
