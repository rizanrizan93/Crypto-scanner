from __future__ import annotations

from decimal import Decimal
from statistics import median

from crypto_scanner.bybit.models import Candle


class TechnicalDataError(ValueError):
    """Raised when candle data is insufficient or internally inconsistent."""


def validate_candles(candles: tuple[Candle, ...], *, min_count: int = 60) -> None:
    if len(candles) < min_count:
        raise TechnicalDataError(f"need at least {min_count} candles, received {len(candles)}")

    previous_timestamp: int | None = None
    for candle in candles:
        if previous_timestamp is not None and candle.start_time_ms <= previous_timestamp:
            raise TechnicalDataError("candle timestamps must be strictly increasing")
        previous_timestamp = candle.start_time_ms
        if min(candle.open, candle.high, candle.low, candle.close) <= 0:
            raise TechnicalDataError("OHLC prices must be positive")
        if candle.high < max(candle.open, candle.close, candle.low):
            raise TechnicalDataError("candle high is inconsistent with OHLC")
        if candle.low > min(candle.open, candle.close, candle.high):
            raise TechnicalDataError("candle low is inconsistent with OHLC")
        if candle.volume < 0 or candle.turnover < 0:
            raise TechnicalDataError("candle volume and turnover cannot be negative")


def closed_candles(
    candles: tuple[Candle, ...],
    *,
    interval_minutes: int,
    now_ms: int,
) -> tuple[Candle, ...]:
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    interval_ms = interval_minutes * 60_000
    return tuple(
        candle for candle in candles if candle.start_time_ms + interval_ms <= now_ms
    )


def ema(values: tuple[Decimal, ...], period: int) -> Decimal:
    if period <= 0:
        raise ValueError("EMA period must be positive")
    if len(values) < period:
        raise TechnicalDataError(f"EMA{period} requires at least {period} values")
    alpha = Decimal(2) / Decimal(period + 1)
    current = sum(values[:period], Decimal(0)) / Decimal(period)
    for value in values[period:]:
        current = value * alpha + current * (Decimal(1) - alpha)
    return current


def rsi(values: tuple[Decimal, ...], period: int = 14) -> Decimal:
    if len(values) <= period:
        raise TechnicalDataError(f"RSI{period} requires more than {period} values")
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for previous, current in zip(values, values[1:], strict=False):
        change = current - previous
        gains.append(max(change, Decimal(0)))
        losses.append(max(-change, Decimal(0)))

    avg_gain = sum(gains[:period], Decimal(0)) / Decimal(period)
    avg_loss = sum(losses[:period], Decimal(0)) / Decimal(period)
    for gain, loss in zip(gains[period:], losses[period:], strict=False):
        avg_gain = (avg_gain * Decimal(period - 1) + gain) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + loss) / Decimal(period)

    if avg_loss == 0:
        return Decimal(100) if avg_gain > 0 else Decimal(50)
    relative_strength = avg_gain / avg_loss
    return Decimal(100) - Decimal(100) / (Decimal(1) + relative_strength)


def true_ranges(candles: tuple[Candle, ...]) -> tuple[Decimal, ...]:
    if len(candles) < 2:
        raise TechnicalDataError("true range requires at least two candles")
    ranges: list[Decimal] = []
    previous_close = candles[0].close
    for candle in candles[1:]:
        ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
        previous_close = candle.close
    return tuple(ranges)


def wilder_average(values: tuple[Decimal, ...], period: int) -> tuple[Decimal, ...]:
    if period <= 0:
        raise ValueError("Wilder period must be positive")
    if len(values) < period:
        raise TechnicalDataError(f"Wilder average requires at least {period} values")
    current = sum(values[:period], Decimal(0)) / Decimal(period)
    output = [current]
    for value in values[period:]:
        current = (current * Decimal(period - 1) + value) / Decimal(period)
        output.append(current)
    return tuple(output)


def atr_series(candles: tuple[Candle, ...], period: int = 14) -> tuple[Decimal, ...]:
    return wilder_average(true_ranges(candles), period)


def atr(candles: tuple[Candle, ...], period: int = 14) -> Decimal:
    return atr_series(candles, period)[-1]


def adx(candles: tuple[Candle, ...], period: int = 14) -> Decimal:
    if len(candles) < period * 2 + 1:
        raise TechnicalDataError(f"ADX{period} requires at least {period * 2 + 1} candles")

    trs: list[Decimal] = []
    plus_dm: list[Decimal] = []
    minus_dm: list[Decimal] = []
    for previous, current in zip(candles, candles[1:], strict=False):
        up_move = current.high - previous.high
        down_move = previous.low - current.low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else Decimal(0))
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else Decimal(0))
        trs.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )

    smoothed_tr = wilder_average(tuple(trs), period)
    smoothed_plus = wilder_average(tuple(plus_dm), period)
    smoothed_minus = wilder_average(tuple(minus_dm), period)
    dx_values: list[Decimal] = []
    for tr_value, plus_value, minus_value in zip(
        smoothed_tr,
        smoothed_plus,
        smoothed_minus,
        strict=True,
    ):
        if tr_value == 0:
            dx_values.append(Decimal(0))
            continue
        plus_di = Decimal(100) * plus_value / tr_value
        minus_di = Decimal(100) * minus_value / tr_value
        denominator = plus_di + minus_di
        dx_values.append(
            Decimal(0)
            if denominator == 0
            else Decimal(100) * abs(plus_di - minus_di) / denominator
        )

    return wilder_average(tuple(dx_values), period)[-1]


def momentum(values: tuple[Decimal, ...], lookback: int = 10) -> Decimal:
    if lookback <= 0 or len(values) <= lookback:
        raise TechnicalDataError("momentum lookback exceeds available values")
    reference = values[-1 - lookback]
    if reference <= 0:
        raise TechnicalDataError("momentum reference price must be positive")
    return values[-1] / reference - Decimal(1)


def atr_expansion_ratio(candles: tuple[Candle, ...], period: int = 14) -> Decimal:
    series = atr_series(candles, period)
    if len(series) < 10:
        raise TechnicalDataError("ATR expansion ratio requires at least ten ATR observations")
    baseline_values = series[-21:-1] if len(series) >= 21 else series[:-1]
    baseline = Decimal(str(median(baseline_values)))
    if baseline <= 0:
        raise TechnicalDataError("ATR baseline must be positive")
    return series[-1] / baseline
