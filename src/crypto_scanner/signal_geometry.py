from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

from crypto_scanner.bybit.models import Candle, InstrumentInfo, TickerSnapshot
from crypto_scanner.discovery import DiscoveryResult, DiscoveryStatus, TradeDirection
from crypto_scanner.strategy_params import DEFAULT_STRATEGY_PARAMETERS, StrategyParameters
from crypto_scanner.structure import (
    StructuralBias,
    StructureEvent,
    StructureState,
    analyze_structure,
)
from crypto_scanner.technical import atr, ema, validate_candles


class EntryMode(StrEnum):
    HL_PULLBACK = "HL_PULLBACK"
    LH_PULLBACK = "LH_PULLBACK"
    MOMENTUM_CONTINUATION = "MOMENTUM_CONTINUATION"
    BREAKOUT_RETEST = "BREAKOUT_RETEST"
    REVERSAL = "REVERSAL"


class GeometryError(RuntimeError):
    """Raised when a candidate cannot produce valid trade geometry."""


@dataclass(frozen=True, slots=True)
class SignalGeometry:
    symbol: str
    direction: TradeDirection
    entry_mode: EntryMode
    entry_price: Decimal
    stop_loss: Decimal
    take_profit_1: Decimal
    take_profit_2: Decimal
    initial_risk: Decimal
    rr_tp1: Decimal
    rr_tp2: Decimal
    reference_swing: Decimal
    breakout_level: Decimal | None
    atr_3m: Decimal
    chase_atr: Decimal


def _round_price(value: Decimal, tick_size: Decimal, *, up: bool) -> Decimal:
    if tick_size <= 0:
        raise GeometryError("instrument tick_size must be positive")
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    steps = (value / tick_size).to_integral_value(rounding=rounding)
    return steps * tick_size


def _candidate_direction(candidate: DiscoveryResult) -> TradeDirection:
    if candidate.status is not DiscoveryStatus.CANDIDATE:
        raise GeometryError("discovery result is not a CANDIDATE")
    if candidate.direction not in {TradeDirection.LONG, TradeDirection.SHORT}:
        raise GeometryError("candidate direction is not tradable")
    return candidate.direction


def _validate_instrument(instrument: InstrumentInfo, symbol: str) -> None:
    if instrument.symbol != symbol:
        raise GeometryError("instrument symbol does not match candidate")
    if instrument.status != "Trading":
        raise GeometryError("instrument is not Trading")
    if instrument.settle_coin != "USDT":
        raise GeometryError("instrument is not USDT settled")
    if instrument.tick_size <= 0 or instrument.qty_step <= 0:
        raise GeometryError("instrument contract precision is invalid")


def _choose_mode(
    direction: TradeDirection,
    candles_3m: tuple[Candle, ...],
    current_price: Decimal,
    atr3: Decimal,
    strategy: StrategyParameters,
) -> tuple[EntryMode, Decimal, Decimal | None]:
    structure = analyze_structure(candles_3m, swing_window=2)
    closes = tuple(candle.close for candle in candles_3m)
    ema20 = ema(closes, 20)

    if direction is TradeDirection.LONG:
        reference = structure.last_swing_low
        breakout = structure.last_swing_high
        if structure.event is StructureEvent.CHOCH_BULLISH:
            return EntryMode.REVERSAL, reference, breakout
        if current_price > breakout:
            distance = current_price - breakout
            if distance <= atr3 * Decimal("0.35"):
                return EntryMode.BREAKOUT_RETEST, reference, breakout
            if distance <= atr3 * strategy.max_chase_atr:
                return EntryMode.MOMENTUM_CONTINUATION, reference, breakout
            raise GeometryError("long quote is chasing too far above breakout")
        valid_pullback = (
            structure.bias is StructuralBias.BULLISH
            and current_price <= ema20 + atr3 * Decimal("0.30")
        )
        if valid_pullback:
            return EntryMode.HL_PULLBACK, reference, None
        raise GeometryError("long structure does not have a valid pullback or continuation")

    reference = structure.last_swing_high
    breakout = structure.last_swing_low
    if structure.event is StructureEvent.CHOCH_BEARISH:
        return EntryMode.REVERSAL, reference, breakout
    if current_price < breakout:
        distance = breakout - current_price
        if distance <= atr3 * Decimal("0.35"):
            return EntryMode.BREAKOUT_RETEST, reference, breakout
        if distance <= atr3 * strategy.max_chase_atr:
            return EntryMode.MOMENTUM_CONTINUATION, reference, breakout
        raise GeometryError("short quote is chasing too far below breakout")
    valid_pullback = (
        structure.bias is StructuralBias.BEARISH
        and current_price >= ema20 - atr3 * Decimal("0.30")
    )
    if valid_pullback:
        return EntryMode.LH_PULLBACK, reference, None
    raise GeometryError("short structure does not have a valid pullback or continuation")


def _structural_targets(
    direction: TradeDirection,
    structure: StructureState,
    entry: Decimal,
    tick_size: Decimal,
) -> tuple[Decimal, Decimal]:
    if direction is TradeDirection.LONG:
        liquidity = sorted({point.price for point in structure.recent_highs if point.price > entry})
        if not liquidity:
            raise GeometryError("no bullish liquidity target above entry")
        tp1 = liquidity[0]
        if len(liquidity) >= 2:
            tp2 = liquidity[1]
        else:
            structural_range = structure.last_swing_high - structure.last_swing_low
            if structural_range <= 0:
                raise GeometryError("invalid bullish structure range")
            tp2 = tp1 + structural_range
        return (
            _round_price(tp1, tick_size, up=False),
            _round_price(tp2, tick_size, up=False),
        )

    liquidity = sorted(
        {point.price for point in structure.recent_lows if point.price < entry},
        reverse=True,
    )
    if not liquidity:
        raise GeometryError("no bearish liquidity target below entry")
    tp1 = liquidity[0]
    if len(liquidity) >= 2:
        tp2 = liquidity[1]
    else:
        structural_range = structure.last_swing_high - structure.last_swing_low
        if structural_range <= 0:
            raise GeometryError("invalid bearish structure range")
        tp2 = tp1 - structural_range
    return (
        _round_price(tp1, tick_size, up=True),
        _round_price(tp2, tick_size, up=True),
    )


def build_signal_geometry(
    candidate: DiscoveryResult,
    *,
    candles_3m: tuple[Candle, ...],
    candles_5m: tuple[Candle, ...],
    ticker: TickerSnapshot,
    instrument: InstrumentInfo,
    strategy: StrategyParameters | None = None,
) -> SignalGeometry:
    strategy = strategy or DEFAULT_STRATEGY_PARAMETERS
    strategy.validate()
    direction = _candidate_direction(candidate)
    symbol = candidate.symbol
    _validate_instrument(instrument, symbol)
    if ticker.symbol != symbol:
        raise GeometryError("ticker symbol does not match candidate")
    if ticker.ask_price <= ticker.bid_price:
        raise GeometryError("ticker bid/ask is invalid")

    validate_candles(candles_3m, min_count=100)
    validate_candles(candles_5m, min_count=100)
    structure5 = analyze_structure(candles_5m, swing_window=2)
    atr3 = atr(candles_3m, 14)
    if atr3 <= 0:
        raise GeometryError("3m ATR must be positive")

    if direction is TradeDirection.LONG:
        if structure5.bias is StructuralBias.BEARISH:
            raise GeometryError("5m structure conflicts with LONG candidate")
        entry = ticker.ask_price
    else:
        if structure5.bias is StructuralBias.BULLISH:
            raise GeometryError("5m structure conflicts with SHORT candidate")
        entry = ticker.bid_price

    mode, reference_swing, breakout_level = _choose_mode(
        direction,
        candles_3m,
        entry,
        atr3,
        strategy,
    )

    stop_buffer = max(instrument.tick_size * Decimal(2), atr3 * strategy.stop_buffer_atr)
    if direction is TradeDirection.LONG:
        stop = _round_price(reference_swing - stop_buffer, instrument.tick_size, up=False)
        risk = entry - stop
        if risk <= 0:
            raise GeometryError("LONG stop is not below entry")
        chase = max(Decimal(0), entry - (breakout_level or entry)) / atr3
    else:
        stop = _round_price(reference_swing + stop_buffer, instrument.tick_size, up=True)
        risk = stop - entry
        if risk <= 0:
            raise GeometryError("SHORT stop is not above entry")
        chase = max(Decimal(0), (breakout_level or entry) - entry) / atr3

    if risk < instrument.tick_size * Decimal(3):
        raise GeometryError("initial risk is too small relative to tick size")

    tp1, tp2 = _structural_targets(
        direction,
        structure5,
        entry,
        instrument.tick_size,
    )

    # Calibration may shorten only an excessively distant structural TP2. It can never
    # move TP2 inside TP1 or below the original 2.00R quality floor.
    if strategy.tp2_cap_rr is not None:
        if direction is TradeDirection.LONG:
            capped = _round_price(
                entry + risk * strategy.tp2_cap_rr,
                instrument.tick_size,
                up=False,
            )
            if tp1 < capped < tp2:
                tp2 = capped
        else:
            capped = _round_price(
                entry - risk * strategy.tp2_cap_rr,
                instrument.tick_size,
                up=True,
            )
            if tp2 < capped < tp1:
                tp2 = capped

    rr1 = abs(tp1 - entry) / risk
    rr2 = abs(tp2 - entry) / risk
    if rr1 < strategy.min_rr_tp1:
        raise GeometryError("nearest structural TP has poor reward/risk")
    if rr2 < strategy.min_rr_tp2:
        raise GeometryError("secondary structural TP has poor reward/risk")
    if direction is TradeDirection.LONG and tp2 <= tp1:
        raise GeometryError("LONG TP2 must remain beyond TP1")
    if direction is TradeDirection.SHORT and tp2 >= tp1:
        raise GeometryError("SHORT TP2 must remain beyond TP1")

    return SignalGeometry(
        symbol=symbol,
        direction=direction,
        entry_mode=mode,
        entry_price=entry,
        stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2,
        initial_risk=risk,
        rr_tp1=rr1,
        rr_tp2=rr2,
        reference_swing=reference_swing,
        breakout_level=breakout_level,
        atr_3m=atr3,
        chase_atr=chase,
    )
