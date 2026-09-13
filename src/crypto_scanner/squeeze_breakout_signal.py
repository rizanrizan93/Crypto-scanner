from __future__ import annotations

from dataclasses import dataclass

from crypto_scanner.binance.models import Candle

UNIVERSE = (
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
    "TRXUSDT", "SUIUSDT", "AAVEUSDT", "UNIUSDT", "ETCUSDT", "NEARUSDT",
    "ATOMUSDT", "XLMUSDT",
)
ATR_PERIOD = 14
PERCENTILE_WINDOW = 90
BREAKOUT_WINDOW = 20
COMPRESSION_FRACTION = 0.20
EXPANSION_MULTIPLIER = 1.5
BAR_MS = 4 * 60 * 60 * 1000


@dataclass(frozen=True, slots=True)
class Setup:
    index: int
    direction: int
    atr: float


def _true_ranges(candles: tuple[Candle, ...]) -> list[float]:
    output: list[float] = []
    previous_close: float | None = None
    for candle in candles:
        high = float(candle.high)
        low = float(candle.low)
        close = float(candle.close)
        tr = high - low
        if previous_close is not None:
            tr = max(tr, abs(high - previous_close), abs(low - previous_close))
        output.append(tr)
        previous_close = close
    return output


def _wilder_atr(true_ranges: list[float]) -> list[float | None]:
    output: list[float | None] = [None] * len(true_ranges)
    if len(true_ranges) < ATR_PERIOD:
        return output
    atr = sum(true_ranges[:ATR_PERIOD]) / ATR_PERIOD
    output[ATR_PERIOD - 1] = atr
    for idx in range(ATR_PERIOD, len(true_ranges)):
        atr = ((ATR_PERIOD - 1) * atr + true_ranges[idx]) / ATR_PERIOD
        output[idx] = atr
    return output


def build_setups(candles: tuple[Candle, ...]) -> tuple[Setup, ...]:
    true_ranges = _true_ranges(candles)
    atrs = _wilder_atr(true_ranges)
    ratios: list[float | None] = []
    for candle, atr in zip(candles, atrs, strict=False):
        close = float(candle.close)
        ratios.append(None if atr is None or close <= 0 else atr / close)

    output: list[Setup] = []
    start = max(ATR_PERIOD - 1 + PERCENTILE_WINDOW, BREAKOUT_WINDOW)
    for idx in range(start, len(candles) - 1):
        if any(
            candles[j].start_time_ms - candles[j - 1].start_time_ms != BAR_MS
            for j in range(idx - PERCENTILE_WINDOW + 1, idx + 1)
        ):
            continue
        atr = atrs[idx]
        ratio = ratios[idx]
        if atr is None or ratio is None or atr <= 0:
            continue
        history = [ratios[j] for j in range(idx - PERCENTILE_WINDOW, idx)]
        if any(value is None for value in history):
            continue
        ordered = sorted(float(value) for value in history if value is not None)
        threshold = ordered[int(COMPRESSION_FRACTION * (len(ordered) - 1))]
        if ratio > threshold:
            continue
        if true_ranges[idx] < EXPANSION_MULTIPLIER * atr:
            continue
        previous = candles[idx - BREAKOUT_WINDOW : idx]
        close = float(candles[idx].close)
        prior_high = max(float(row.high) for row in previous)
        prior_low = min(float(row.low) for row in previous)
        if close > prior_high:
            output.append(Setup(idx, 1, atr))
        elif close < prior_low:
            output.append(Setup(idx, -1, atr))
    return tuple(output)
