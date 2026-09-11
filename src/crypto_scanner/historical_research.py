from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from crypto_scanner.binance.models import Candle
from crypto_scanner.historical_replay import (
    ImpulseRetest,
    replay_calibrated_geometry,
    replay_fixed_geometry,
)
from crypto_scanner.strategy_params import StrategyParameters
from crypto_scanner.technical import validate_candles


@dataclass(frozen=True, slots=True)
class HistoricalResearchTrade:
    decision_time_ms: int
    entry_time_ms: int
    direction: str
    impulse_index: int
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    gross_result_r: Decimal
    net_result_r: Decimal
    mfe_r: Decimal
    mae_r: Decimal
    exit_reason: str
    symbol: str = ""


@dataclass(frozen=True, slots=True)
class HistoricalResearchSummary:
    sample_size: int
    wins: int
    losses: int
    win_rate: Decimal
    average_gross_r: Decimal
    average_net_r: Decimal
    median_net_r: Decimal
    profit_factor_r: Decimal | None
    average_mfe_r: Decimal
    average_mae_r: Decimal


def _net_after_round_trip_cost(
    gross_r: Decimal,
    *,
    entry_price: Decimal,
    risk: Decimal,
    round_trip_cost_bps: Decimal,
) -> Decimal:
    cost_price = entry_price * round_trip_cost_bps / Decimal("10000")
    return gross_r - cost_price / risk


def _ema20_by_index(candles: tuple[Candle, ...]) -> tuple[Decimal | None, ...]:
    result: list[Decimal | None] = [None] * len(candles)
    closes = tuple(candle.close for candle in candles)
    current = sum(closes[:20], Decimal(0)) / Decimal(20)
    result[19] = current
    alpha = Decimal(2) / Decimal(21)
    for index in range(20, len(closes)):
        current = closes[index] * alpha + current * (Decimal(1) - alpha)
        result[index] = current
    return tuple(result)


def _atr14_by_index(candles: tuple[Candle, ...]) -> tuple[Decimal | None, ...]:
    result: list[Decimal | None] = [None] * len(candles)
    ranges = tuple(
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in zip(candles, candles[1:], strict=False)
    )
    current = sum(ranges[:14], Decimal(0)) / Decimal(14)
    result[14] = current
    for range_index in range(14, len(ranges)):
        current = (current * Decimal(13) + ranges[range_index]) / Decimal(14)
        result[range_index + 1] = current
    return tuple(result)


def _detect_precomputed_impulse_retest(
    candles: tuple[Candle, ...],
    *,
    decision: int,
    atr14_by_index: tuple[Decimal | None, ...],
    ema20_by_index: tuple[Decimal | None, ...],
    impulse_atr: Decimal,
    retest_tolerance_atr: Decimal,
    max_retest_bars: int,
) -> ImpulseRetest | None:
    start = max(20, decision - max_retest_bars)
    retest = candles[decision]
    for impulse_idx in range(decision - 1, start - 1, -1):
        atr14 = atr14_by_index[impulse_idx]
        ema20 = ema20_by_index[impulse_idx]
        if atr14 is None or ema20 is None or atr14 <= 0:
            continue
        impulse = candles[impulse_idx]
        body = abs(impulse.close - impulse.open)
        if body < atr14 * impulse_atr:
            continue
        recent = candles[max(0, impulse_idx - 8) : impulse_idx]
        bullish_level = max(candle.high for candle in recent)
        bearish_level = min(candle.low for candle in recent)
        tolerance = atr14 * retest_tolerance_atr
        intervening = candles[impulse_idx + 1 : decision]
        bullish_invalidation = bullish_level - tolerance
        if (
            impulse.close > bullish_level
            and impulse.close > ema20
            and not any(candle.close < bullish_invalidation for candle in intervening)
            and retest.low <= bullish_level + tolerance
            and retest.low >= bullish_invalidation
            and retest.close >= bullish_level
            and retest.close > retest.open
        ):
            return ImpulseRetest(
                "LONG",
                impulse_idx,
                decision,
                bullish_level,
                body / atr14,
                max(Decimal(0), bullish_level - retest.low) / atr14,
            )
        bearish_invalidation = bearish_level + tolerance
        if (
            impulse.close < bearish_level
            and impulse.close < ema20
            and not any(candle.close > bearish_invalidation for candle in intervening)
            and retest.high >= bearish_level - tolerance
            and retest.high <= bearish_invalidation
            and retest.close <= bearish_level
            and retest.close < retest.open
        ):
            return ImpulseRetest(
                "SHORT",
                impulse_idx,
                decision,
                bearish_level,
                body / atr14,
                max(Decimal(0), retest.high - bearish_level) / atr14,
            )
    return None


def replay_impulse_retest_research(
    candles: tuple[Candle, ...],
    *,
    horizon_bars: int = 20,
    stop_buffer_atr: Decimal = Decimal("0.15"),
    target_r: Decimal = Decimal("2.00"),
    impulse_atr: Decimal = Decimal("1.20"),
    retest_tolerance_atr: Decimal = Decimal("0.30"),
    max_retest_bars: int = 6,
    round_trip_cost_bps: Decimal = Decimal("0"),
    strategy: StrategyParameters | None = None,
    symbol: str = "",
) -> tuple[HistoricalResearchTrade, ...]:
    """Replay impulse-retest signals without contaminating forward Demo evidence.

    Signals are detected only after a decision candle closes. Entry is frozen at the
    next candle open. Each impulse contributes at most one sample. Outcomes use only
    subsequent candles and conservative SL-first handling for intrabar ambiguity.
    """
    validate_candles(candles, min_count=60)
    if horizon_bars < 1:
        raise ValueError("horizon_bars must be positive")
    if stop_buffer_atr < 0:
        raise ValueError("stop_buffer_atr must be non-negative")
    if target_r <= 0:
        raise ValueError("target_r must be positive")
    if round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps must be non-negative")
    if strategy is not None:
        strategy.validate()
        stop_buffer_atr = strategy.stop_buffer_atr
        target_r = strategy.tp2_cap_rr or strategy.min_rr_tp2

    trades: list[HistoricalResearchTrade] = []
    used_impulses: set[int] = set()
    latest_decision = len(candles) - horizon_bars - 2
    atr14_by_index = _atr14_by_index(candles)
    ema20_by_index = _ema20_by_index(candles)

    for decision_idx in range(39, latest_decision + 1):
        signal = _detect_precomputed_impulse_retest(
            candles,
            decision=decision_idx,
            atr14_by_index=atr14_by_index,
            ema20_by_index=ema20_by_index,
            impulse_atr=impulse_atr,
            retest_tolerance_atr=retest_tolerance_atr,
            max_retest_bars=max_retest_bars,
        )
        if signal is None or signal.impulse_index in used_impulses:
            continue

        atr14 = atr14_by_index[decision_idx]
        if atr14 is None or atr14 <= 0:
            continue
        decision = candles[decision_idx]
        entry_bar = candles[decision_idx + 1]
        entry = entry_bar.open
        chase_atr = abs(entry - signal.impulse_level) / atr14
        if strategy is not None and chase_atr > strategy.max_chase_atr:
            continue
        buffer = atr14 * stop_buffer_atr

        if signal.direction == "LONG":
            stop = decision.low - buffer
            risk = entry - stop
            if risk <= 0:
                continue
            target = entry + risk * target_r
        else:
            stop = decision.high + buffer
            risk = stop - entry
            if risk <= 0:
                continue
            target = entry - risk * target_r

        future = candles[
            decision_idx + 1 : decision_idx + 1 + horizon_bars
        ]
        replay = replay_fixed_geometry if strategy is None else replay_calibrated_geometry
        replay_kwargs = {
            "direction": signal.direction,
            "entry_price": entry,
            "stop_loss": stop,
            "take_profit": target,
        }
        if strategy is None:
            outcome = replay(future, **replay_kwargs)
        else:
            outcome = replay(future, strategy=strategy, **replay_kwargs)
        net_r = _net_after_round_trip_cost(
            outcome.result_r,
            entry_price=entry,
            risk=risk,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        trades.append(
            HistoricalResearchTrade(
                decision_time_ms=decision.start_time_ms,
                entry_time_ms=entry_bar.start_time_ms,
                direction=signal.direction,
                impulse_index=signal.impulse_index,
                entry_price=entry,
                stop_loss=stop,
                take_profit=target,
                gross_result_r=outcome.result_r,
                net_result_r=net_r,
                mfe_r=outcome.mfe_r,
                mae_r=outcome.mae_r,
                exit_reason=outcome.exit_reason,
                symbol=symbol,
            )
        )
        used_impulses.add(signal.impulse_index)

    return tuple(trades)


def summarize_historical_research(
    trades: tuple[HistoricalResearchTrade, ...],
) -> HistoricalResearchSummary:
    if not trades:
        return HistoricalResearchSummary(
            sample_size=0,
            wins=0,
            losses=0,
            win_rate=Decimal(0),
            average_gross_r=Decimal(0),
            average_net_r=Decimal(0),
            median_net_r=Decimal(0),
            profit_factor_r=None,
            average_mfe_r=Decimal(0),
            average_mae_r=Decimal(0),
        )

    count = Decimal(len(trades))
    wins = sum(trade.net_result_r > 0 for trade in trades)
    losses = sum(trade.net_result_r < 0 for trade in trades)
    gross_profit = sum(
        (trade.net_result_r for trade in trades if trade.net_result_r > 0),
        Decimal(0),
    )
    gross_loss = abs(
        sum(
            (trade.net_result_r for trade in trades if trade.net_result_r < 0),
            Decimal(0),
        )
    )
    return HistoricalResearchSummary(
        sample_size=len(trades),
        wins=wins,
        losses=losses,
        win_rate=Decimal(wins) / count,
        average_gross_r=sum((t.gross_result_r for t in trades), Decimal(0)) / count,
        average_net_r=sum((t.net_result_r for t in trades), Decimal(0)) / count,
        median_net_r=Decimal(str(median(t.net_result_r for t in trades))),
        profit_factor_r=gross_profit / gross_loss if gross_loss > 0 else None,
        average_mfe_r=sum((t.mfe_r for t in trades), Decimal(0)) / count,
        average_mae_r=sum((t.mae_r for t in trades), Decimal(0)) / count,
    )
