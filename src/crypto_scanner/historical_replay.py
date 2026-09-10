from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from crypto_scanner.binance.models import Candle
from crypto_scanner.profit_lock import _safe_locked_r, locked_r_from_mfe
from crypto_scanner.strategy_params import StrategyParameters
from crypto_scanner.technical import atr, ema, validate_candles


@dataclass(frozen=True, slots=True)
class ImpulseRetest:
    direction: str
    impulse_index: int
    retest_index: int
    impulse_level: Decimal
    impulse_range_atr: Decimal
    retest_depth_atr: Decimal


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    direction: str
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    exit_price: Decimal
    result_r: Decimal
    mfe_r: Decimal
    mae_r: Decimal
    exit_reason: str


def detect_impulse_retest(
    candles: tuple[Candle, ...],
    *,
    impulse_atr: Decimal = Decimal("1.20"),
    retest_tolerance_atr: Decimal = Decimal("0.30"),
    max_retest_bars: int = 6,
) -> ImpulseRetest | None:
    """Detect a completed impulse followed by a bounded retest without future leakage.

    The final candle is the decision candle. Context and the impulse use only candles
    available before that decision. A valid retest may probe the broken level only by
    the configured ATR tolerance; any earlier post-impulse close through the invalidation
    boundary cancels the setup.
    """
    validate_candles(candles, min_count=40)
    if impulse_atr <= 0:
        raise ValueError("impulse_atr must be positive")
    if retest_tolerance_atr < 0:
        raise ValueError("retest_tolerance_atr must be non-negative")
    if max_retest_bars < 1:
        raise ValueError("max_retest_bars must be positive")

    decision = len(candles) - 1
    start = max(20, decision - max_retest_bars)
    retest = candles[decision]

    for impulse_idx in range(decision - 1, start - 1, -1):
        history = candles[: impulse_idx + 1]
        atr14 = atr(history, 14)
        if atr14 <= 0:
            continue
        impulse = candles[impulse_idx]
        body = abs(impulse.close - impulse.open)
        if body < atr14 * impulse_atr:
            continue

        closes = tuple(c.close for c in history)
        ema20 = ema(closes, 20)
        recent = candles[max(0, impulse_idx - 8) : impulse_idx]
        if not recent:
            continue

        bullish_level = max(c.high for c in recent)
        bearish_level = min(c.low for c in recent)
        tolerance = atr14 * retest_tolerance_atr
        intervening = candles[impulse_idx + 1 : decision]

        bullish_impulse = impulse.close > bullish_level and impulse.close > ema20
        if bullish_impulse:
            invalidation = bullish_level - tolerance
            broken_before_decision = any(c.close < invalidation for c in intervening)
            touched = retest.low <= bullish_level + tolerance
            bounded = retest.low >= invalidation
            held = retest.close >= bullish_level and retest.close > retest.open
            if not broken_before_decision and touched and bounded and held:
                return ImpulseRetest(
                    direction="LONG",
                    impulse_index=impulse_idx,
                    retest_index=decision,
                    impulse_level=bullish_level,
                    impulse_range_atr=body / atr14,
                    retest_depth_atr=max(Decimal(0), bullish_level - retest.low) / atr14,
                )

        bearish_impulse = impulse.close < bearish_level and impulse.close < ema20
        if bearish_impulse:
            invalidation = bearish_level + tolerance
            broken_before_decision = any(c.close > invalidation for c in intervening)
            touched = retest.high >= bearish_level - tolerance
            bounded = retest.high <= invalidation
            held = retest.close <= bearish_level and retest.close < retest.open
            if not broken_before_decision and touched and bounded and held:
                return ImpulseRetest(
                    direction="SHORT",
                    impulse_index=impulse_idx,
                    retest_index=decision,
                    impulse_level=bearish_level,
                    impulse_range_atr=body / atr14,
                    retest_depth_atr=max(Decimal(0), retest.high - bearish_level) / atr14,
                )
    return None


def replay_fixed_geometry(
    future_candles: tuple[Candle, ...],
    *,
    direction: str,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
) -> ReplayOutcome:
    """Evaluate frozen trade geometry on subsequent candles only.

    If SL and TP are both touched in one candle, the conservative SL-first assumption
    is used. This avoids optimistic intrabar look-ahead in coarse historical candles.
    """
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    if direction == "LONG" and not stop_loss < entry_price < take_profit:
        raise ValueError("LONG geometry must satisfy stop < entry < target")
    if direction == "SHORT" and not stop_loss > entry_price > take_profit:
        raise ValueError("SHORT geometry must satisfy stop > entry > target")

    risk = abs(entry_price - stop_loss)
    if risk <= 0:
        raise ValueError("initial risk must be positive")

    mfe = Decimal(0)
    mae = Decimal(0)
    for candle in future_candles:
        if direction == "LONG":
            mfe = max(mfe, (candle.high - entry_price) / risk)
            mae = max(mae, (entry_price - candle.low) / risk)
            hit_sl = candle.low <= stop_loss
            hit_tp = candle.high >= take_profit
        else:
            mfe = max(mfe, (entry_price - candle.low) / risk)
            mae = max(mae, (candle.high - entry_price) / risk)
            hit_sl = candle.high >= stop_loss
            hit_tp = candle.low <= take_profit

        if hit_sl:
            return ReplayOutcome(
                direction,
                entry_price,
                stop_loss,
                take_profit,
                stop_loss,
                Decimal(-1),
                mfe,
                mae,
                "SL",
            )
        if hit_tp:
            rr = abs(take_profit - entry_price) / risk
            return ReplayOutcome(
                direction,
                entry_price,
                stop_loss,
                take_profit,
                take_profit,
                rr,
                mfe,
                mae,
                "TP",
            )

    exit_price = future_candles[-1].close if future_candles else entry_price
    signed = (exit_price - entry_price) if direction == "LONG" else (entry_price - exit_price)
    return ReplayOutcome(
        direction,
        entry_price,
        stop_loss,
        take_profit,
        exit_price,
        signed / risk,
        mfe,
        mae,
        "HORIZON",
    )


def replay_calibrated_geometry(
    future_candles: tuple[Candle, ...],
    *,
    direction: str,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    strategy: StrategyParameters,
) -> ReplayOutcome:
    """Replay TP2 and production profit-lock without intrabar look-ahead."""
    strategy.validate()
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    if direction == "LONG" and not stop_loss < entry_price < take_profit:
        raise ValueError("LONG geometry must satisfy stop < entry < target")
    if direction == "SHORT" and not stop_loss > entry_price > take_profit:
        raise ValueError("SHORT geometry must satisfy stop > entry > target")
    risk = abs(entry_price - stop_loss)
    active_stop = stop_loss
    mfe = mae = Decimal(0)
    for candle in future_candles:
        if direction == "LONG":
            hit_sl = candle.low <= active_stop
            hit_tp = candle.high >= take_profit
            candle_mfe = (candle.high - entry_price) / risk
            candle_mae = (entry_price - candle.low) / risk
        else:
            hit_sl = candle.high >= active_stop
            hit_tp = candle.low <= take_profit
            candle_mfe = (entry_price - candle.low) / risk
            candle_mae = (candle.high - entry_price) / risk
        mfe = max(mfe, candle_mfe)
        mae = max(mae, candle_mae)
        if hit_sl:
            result_r = (
                (active_stop - entry_price) / risk
                if direction == "LONG"
                else (entry_price - active_stop) / risk
            )
            return ReplayOutcome(
                direction,
                entry_price,
                stop_loss,
                take_profit,
                active_stop,
                result_r,
                mfe,
                mae,
                "SL" if active_stop == stop_loss else "PROFIT_LOCK",
            )
        if hit_tp:
            return ReplayOutcome(
                direction,
                entry_price,
                stop_loss,
                take_profit,
                take_profit,
                abs(take_profit - entry_price) / risk,
                mfe,
                mae,
                "TP",
            )
        target_lock = locked_r_from_mfe(mfe, strategy)
        if target_lock is None:
            continue
        current_r = (
            (candle.close - entry_price) / risk
            if direction == "LONG"
            else (entry_price - candle.close) / risk
        )
        safe_lock = _safe_locked_r(target_locked_r=target_lock, current_r=current_r)
        if safe_lock is None:
            continue
        proposed = (
            entry_price + safe_lock * risk
            if direction == "LONG"
            else entry_price - safe_lock * risk
        )
        active_stop = (
            max(active_stop, proposed)
            if direction == "LONG"
            else min(active_stop, proposed)
        )
    exit_price = future_candles[-1].close if future_candles else entry_price
    signed = (
        exit_price - entry_price
        if direction == "LONG"
        else entry_price - exit_price
    )
    return ReplayOutcome(
        direction,
        entry_price,
        stop_loss,
        take_profit,
        exit_price,
        signed / risk,
        mfe,
        mae,
        "HORIZON",
    )
