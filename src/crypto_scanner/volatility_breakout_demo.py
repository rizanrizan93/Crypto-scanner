from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from decimal import Decimal

from crypto_scanner.binance.models import Candle
from crypto_scanner.discovery import DiscoveryResult, TradeDirection
from crypto_scanner.regime_specialist_demo import DAY_MS, DailyKlineSource

VOLATILITY_BREAKOUT_STRATEGY_ID = "strategy-vol-breakout-30-15-vt20-v1"
ENTRY_LOOKBACK = 30
EXIT_LOOKBACK = 15
EMA_PERIOD = 200
COMPRESSION_FAST = 20
COMPRESSION_SLOW = 120
COMPRESSION_MAX_RATIO = 0.85
INVOL_LOOKBACK = 20
VOL_TARGET_LOOKBACK = 60
TARGET_ANNUAL_VOL = 0.20
# Stop-risk budget for the entire breakout sleeve. The shared account gate still
# independently caps portfolio risk at 5% and each trade at <= 1%.
SLEEVE_STOP_RISK_BUDGET = Decimal("0.005")


@dataclass(frozen=True, slots=True)
class VolatilityBreakoutLeg:
    symbol: str
    direction: TradeDirection
    target_weight: Decimal
    risk_fraction: Decimal


@dataclass(frozen=True, slots=True)
class VolatilityBreakoutDecision:
    as_of_ms: int
    observed_annual_vol: Decimal
    vol_scale: Decimal
    legs: tuple[VolatilityBreakoutLeg, ...]


def _closed_daily(candles: tuple[Candle, ...], *, now_ms: int) -> tuple[Candle, ...]:
    return tuple(
        candle
        for candle in sorted(candles, key=lambda item: item.start_time_ms)
        if candle.start_time_ms + DAY_MS <= now_ms
    )


def _ema_at(closes: tuple[Decimal, ...], index: int, period: int) -> Decimal:
    if index < period - 1:
        raise ValueError("insufficient observations for EMA")
    current = sum(closes[:period], Decimal(0)) / Decimal(period)
    alpha = Decimal(2) / Decimal(period + 1)
    for value in closes[period : index + 1]:
        current = value * alpha + current * (Decimal(1) - alpha)
    return current


def _returns_at(candles: tuple[Candle, ...], index: int, lookback: int) -> tuple[float, ...]:
    if index < lookback:
        return ()
    rows = candles[index - lookback : index + 1]
    values: list[float] = []
    for previous, current in zip(rows, rows[1:], strict=False):
        if previous.close <= 0:
            continue
        values.append(float(current.close / previous.close - Decimal(1)))
    return tuple(values)


def _daily_vol(candles: tuple[Candle, ...], index: int, lookback: int) -> float | None:
    values = _returns_at(candles, index, lookback)
    if len(values) < max(10, lookback // 2):
        return None
    value = statistics.pstdev(values)
    return value if value > 1e-12 else None


def replay_vol30_state(candles: tuple[Candle, ...]) -> int:
    """Reconstruct the frozen 30/15 volatility-breakout state from closed D1 bars.

    The rules mirror the research definition: 30-day high/low entry, 15-day
    opposite-channel exit, EMA200 direction filter and 20d/120d volatility
    compression <= 0.85. Return values are +1 LONG, -1 SHORT and 0 FLAT.
    """

    if len(candles) < EMA_PERIOD + 1:
        return 0
    closes = tuple(candle.close for candle in candles)
    state = 0
    start = max(EMA_PERIOD, COMPRESSION_SLOW)
    for index in range(start, len(candles)):
        bar = candles[index]
        prior_entry = candles[index - ENTRY_LOOKBACK : index]
        prior_exit = candles[index - EXIT_LOOKBACK : index]
        if len(prior_entry) < ENTRY_LOOKBACK or len(prior_exit) < EXIT_LOOKBACK:
            continue
        ema200 = _ema_at(closes, index, EMA_PERIOD)
        vol20 = _daily_vol(candles, index, COMPRESSION_FAST)
        vol120 = _daily_vol(candles, index, COMPRESSION_SLOW)
        compression_ok = bool(
            vol20 is not None
            and vol120 is not None
            and vol120 > 0
            and vol20 / vol120 <= COMPRESSION_MAX_RATIO
        )

        if state == 0:
            if (
                bar.close > max(item.high for item in prior_entry)
                and bar.close > ema200
                and compression_ok
            ):
                state = 1
            elif (
                bar.close < min(item.low for item in prior_entry)
                and bar.close < ema200
                and compression_ok
            ):
                state = -1
        elif state > 0:
            if bar.close < min(item.low for item in prior_exit) or bar.close < ema200:
                state = 0
        elif bar.close > max(item.high for item in prior_exit) or bar.close > ema200:
            state = 0
    return state


def _fixed_weight_portfolio_vol(
    histories: dict[str, tuple[Candle, ...]],
    signed_weights: dict[str, float],
    *,
    as_of_ms: int,
) -> float | None:
    if not signed_weights:
        return None
    return_maps: dict[str, dict[int, float]] = {}
    timestamps: set[int] = set()
    for symbol, candles in histories.items():
        rows = tuple(candle for candle in candles if candle.start_time_ms <= as_of_ms)
        mapping: dict[int, float] = {}
        for previous, current in zip(rows, rows[1:], strict=False):
            if previous.close <= 0:
                continue
            mapping[current.start_time_ms] = float(current.close / previous.close - Decimal(1))
        return_maps[symbol] = mapping
        timestamps.update(mapping)

    recent_candidates = sorted(
        timestamp for timestamp in timestamps if timestamp <= as_of_ms
    )
    recent = recent_candidates[-VOL_TARGET_LOOKBACK:]
    portfolio_returns: list[float] = []
    for timestamp in recent:
        total = 0.0
        observed = False
        for symbol, weight in signed_weights.items():
            value = return_maps.get(symbol, {}).get(timestamp)
            if value is None:
                continue
            total += weight * value
            observed = True
        if observed:
            portfolio_returns.append(total)
    if len(portfolio_returns) < max(30, VOL_TARGET_LOOKBACK // 2):
        return None
    daily = statistics.pstdev(portfolio_returns)
    if daily <= 1e-12:
        return None
    return daily * math.sqrt(365)


def build_volatility_breakout_decision(
    source: DailyKlineSource,
    universe: tuple[str, ...],
    *,
    now_ms: int,
) -> VolatilityBreakoutDecision:
    histories: dict[str, tuple[Candle, ...]] = {}
    states: dict[str, int] = {}
    as_of_ms = 0

    for raw_symbol in universe:
        symbol = raw_symbol.upper()
        candles = _closed_daily(source.get_klines(symbol, "D", limit=360), now_ms=now_ms)
        if len(candles) < EMA_PERIOD + 1:
            continue
        histories[symbol] = candles
        as_of_ms = max(as_of_ms, candles[-1].start_time_ms)
        state = replay_vol30_state(candles)
        if state:
            states[symbol] = state

    if not states:
        return VolatilityBreakoutDecision(as_of_ms, Decimal(0), Decimal(0), ())

    inverse_vol: dict[str, float] = {}
    for symbol in states:
        candles = histories[symbol]
        vol = _daily_vol(candles, len(candles) - 1, INVOL_LOOKBACK)
        if vol is not None and vol > 0:
            inverse_vol[symbol] = 1.0 / vol
    total_inverse = sum(inverse_vol.values())
    if total_inverse <= 0:
        return VolatilityBreakoutDecision(as_of_ms, Decimal(0), Decimal(0), ())

    base = {symbol: raw / total_inverse for symbol, raw in inverse_vol.items()}
    signed = {symbol: base[symbol] * states[symbol] for symbol in base}
    observed = _fixed_weight_portfolio_vol(histories, signed, as_of_ms=as_of_ms)
    if observed is None or observed <= 0:
        return VolatilityBreakoutDecision(as_of_ms, Decimal(0), Decimal(0), ())
    scale = min(1.0, TARGET_ANNUAL_VOL / observed)

    legs: list[VolatilityBreakoutLeg] = []
    for symbol, weight in signed.items():
        target = weight * scale
        risk_fraction = SLEEVE_STOP_RISK_BUDGET * Decimal(str(abs(target)))
        direction = TradeDirection.LONG if target > 0 else TradeDirection.SHORT
        legs.append(
            VolatilityBreakoutLeg(
                symbol=symbol,
                direction=direction,
                target_weight=Decimal(str(target)),
                risk_fraction=risk_fraction,
            )
        )
    legs.sort(key=lambda leg: (-abs(leg.target_weight), leg.symbol))
    return VolatilityBreakoutDecision(
        as_of_ms=as_of_ms,
        observed_annual_vol=Decimal(str(observed)),
        vol_scale=Decimal(str(scale)),
        legs=tuple(legs),
    )


def filter_volatility_breakout_candidates(
    candidates: tuple[DiscoveryResult, ...],
    decision: VolatilityBreakoutDecision,
) -> tuple[DiscoveryResult, ...]:
    allowed = {(leg.symbol, leg.direction) for leg in decision.legs}
    rank = {(leg.symbol, leg.direction): index for index, leg in enumerate(decision.legs)}
    selected = [
        candidate
        for candidate in candidates
        if (candidate.symbol, candidate.direction) in allowed
    ]
    selected.sort(key=lambda item: rank[(item.symbol, item.direction)])
    return tuple(selected)
