from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from crypto_scanner.adaptive_d1_data import DailyBar
from crypto_scanner.binance.models import Candle

DAY_MS = 86_400_000
H4_MS = 14_400_000


@dataclass(frozen=True, slots=True)
class RegimePoint:
    effective_ms: int
    state: int


@dataclass(frozen=True, slots=True)
class Setup:
    index: int
    direction: int
    atr: float


def ema(values: list[float], span: int) -> list[float]:
    alpha = 2.0 / (span + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def build_regimes(rows: tuple[DailyBar, ...]) -> tuple[RegimePoint, ...]:
    closes = [row.close for row in rows]
    ema200 = ema(closes, 200)
    out: list[RegimePoint] = []
    for idx in range(200, len(rows)):
        if any(rows[j].start_time_ms - rows[j - 1].start_time_ms != DAY_MS for j in range(idx - 200, idx + 1)):
            continue
        momentum = closes[idx] / closes[idx - 60] - 1.0
        state = 0
        if closes[idx] > ema200[idx] and momentum > 0:
            state = 1
        elif closes[idx] < ema200[idx] and momentum < 0:
            state = -1
        out.append(RegimePoint(rows[idx].start_time_ms + DAY_MS, state))
    return tuple(out)


def _atr(candles: tuple[Candle, ...]) -> list[float | None]:
    tr: list[float] = []
    prior_close: float | None = None
    for candle in candles:
        high = float(candle.high)
        low = float(candle.low)
        close = float(candle.close)
        value = high - low
        if prior_close is not None:
            value = max(value, abs(high - prior_close), abs(low - prior_close))
        tr.append(value)
        prior_close = close
    out: list[float | None] = [None] * len(tr)
    if len(tr) < 14:
        return out
    value = sum(tr[:14]) / 14.0
    out[13] = value
    for idx in range(14, len(tr)):
        value = (13.0 * value + tr[idx]) / 14.0
        out[idx] = value
    return out


def build_setups(candles: tuple[Candle, ...], regimes: tuple[RegimePoint, ...]) -> tuple[Setup, ...]:
    closes = [float(row.close) for row in candles]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    atr14 = _atr(candles)
    times = [point.effective_ms for point in regimes]
    out: list[Setup] = []
    for idx in range(50, len(candles) - 1):
        if any(candles[j].start_time_ms - candles[j - 1].start_time_ms != H4_MS for j in range(idx - 50, idx + 1)):
            continue
        atr = atr14[idx]
        if atr is None or atr <= 0:
            continue
        signal_close = candles[idx].start_time_ms + H4_MS
        pos = bisect_right(times, signal_close) - 1
        if pos < 0:
            continue
        regime = regimes[pos].state
        row = candles[idx]
        open_ = float(row.open)
        high = float(row.high)
        low = float(row.low)
        close = float(row.close)
        if regime == 1 and ema20[idx] > ema50[idx] and low <= ema20[idx] and close > ema20[idx] and close > open_:
            out.append(Setup(idx, 1, atr))
        elif regime == -1 and ema20[idx] < ema50[idx] and high >= ema20[idx] and close < ema20[idx] and close < open_:
            out.append(Setup(idx, -1, atr))
    return tuple(out)
