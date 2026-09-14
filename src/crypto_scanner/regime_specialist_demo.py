from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from crypto_scanner.binance.models import Candle
from crypto_scanner.discovery import DiscoveryResult, TradeDirection

REGIME_SPECIALIST_STRATEGY_ID = "strategy-regime-specialist-28d25-v1"
BULL_RISK_FRACTION = Decimal("0.005")
BEAR_RISK_FRACTION = Decimal("0.00125")
DAY_MS = 86_400_000


class GlobalRegime(StrEnum):
    BULL = "BULL"
    BEAR = "BEAR"
    TRANSITION = "TRANSITION"


@dataclass(frozen=True, slots=True)
class RegimeSpecialistDecision:
    regime: GlobalRegime
    as_of_ms: int
    bull_switch_active: bool
    bear_short_symbols: tuple[str, ...]

    @property
    def risk_fraction(self) -> Decimal:
        return BULL_RISK_FRACTION if self.regime is GlobalRegime.BULL else BEAR_RISK_FRACTION


class DailyKlineSource(Protocol):
    def get_klines(self, symbol: str, interval: str, *, limit: int = 200) -> tuple[Candle, ...]: ...


def _closed_daily(candles: tuple[Candle, ...], *, now_ms: int) -> tuple[Candle, ...]:
    return tuple(
        candle
        for candle in sorted(candles, key=lambda item: item.start_time_ms)
        if candle.start_time_ms + DAY_MS <= now_ms
    )


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        raise ValueError("mean requires observations")
    return sum(values, Decimal(0)) / Decimal(len(values))


def _population_std(values: tuple[Decimal, ...]) -> Decimal:
    mean = _mean(values)
    variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / Decimal(len(values))
    return variance.sqrt()


def _ema(values: tuple[Decimal, ...], period: int) -> Decimal:
    if period < 1 or len(values) < period:
        raise ValueError("insufficient observations for EMA")
    ema = _mean(values[:period])
    alpha = Decimal(2) / Decimal(period + 1)
    for value in values[period:]:
        ema = value * alpha + ema * (Decimal(1) - alpha)
    return ema


def classify_global_regime(closes: tuple[Decimal, ...]) -> GlobalRegime:
    if len(closes) < 200:
        raise ValueError("global regime requires at least 200 closed daily candles")
    close = closes[-1]
    ema200 = _ema(closes, 200)
    momentum60 = close / closes[-61] - Decimal(1) if len(closes) >= 61 else Decimal(0)
    if close > ema200 and momentum60 > 0:
        return GlobalRegime.BULL
    if close < ema200 and momentum60 < 0:
        return GlobalRegime.BEAR
    return GlobalRegime.TRANSITION


def bull_regime_switch_active(closes: tuple[Decimal, ...]) -> bool:
    """Reconstruct the frozen long/cash Regime Switch state from closed daily prices."""
    if len(closes) < 201:
        return False

    mode: str | None = None
    trend_active = False
    range_active = False
    selected_active = False

    for index in range(200, len(closes)):
        close = closes[index]
        er_path = closes[index - 20 : index + 1]
        direction = abs(er_path[-1] - er_path[0])
        travelled = sum(
            (abs(er_path[pos] - er_path[pos - 1]) for pos in range(1, len(er_path))),
            Decimal(0),
        )
        er20 = direction / travelled if travelled > 0 else Decimal(0)
        if er20 >= Decimal("0.30"):
            mode = "TREND"
        elif er20 <= Decimal("0.20"):
            mode = "RANGE"

        prior55 = closes[index - 55 : index]
        prior20 = closes[index - 20 : index]
        if close > max(prior55):
            trend_active = True
        elif close < min(prior20):
            trend_active = False

        boll20 = closes[index - 19 : index + 1]
        boll_mean = _mean(boll20)
        boll_std = _population_std(boll20)
        if close < boll_mean - Decimal(2) * boll_std:
            range_active = True
        elif close >= boll_mean:
            range_active = False

        sma200 = _mean(closes[index - 199 : index + 1])
        large_direction_ok = close > sma200
        selected_active = large_direction_ok and (
            (mode == "TREND" and trend_active)
            or (mode == "RANGE" and range_active)
        )

    return selected_active


def _latest_completed_sunday_start(candles: tuple[Candle, ...]) -> int | None:
    sundays = tuple(
        candle.start_time_ms
        for candle in candles
        if datetime.fromtimestamp(candle.start_time_ms / 1000, tz=UTC).weekday() == 6
    )
    return max(sundays) if sundays else None


def _return_28d_at_anchor(candles: tuple[Candle, ...], anchor_ms: int) -> Decimal | None:
    history = tuple(candle for candle in candles if candle.start_time_ms <= anchor_ms)
    if len(history) < 200:
        return None
    if history[-1].start_time_ms != anchor_ms or len(history) < 29:
        return None
    old = history[-29].close
    if old <= 0:
        return None
    return history[-1].close / old - Decimal(1)


def build_regime_specialist_decision(
    source: DailyKlineSource,
    universe: tuple[str, ...],
    *,
    now_ms: int,
) -> RegimeSpecialistDecision:
    btc = _closed_daily(source.get_klines("BTCUSDT", "D", limit=320), now_ms=now_ms)
    if len(btc) < 201:
        raise ValueError("BTC daily history is insufficient for regime specialist")
    closes = tuple(candle.close for candle in btc)
    regime = classify_global_regime(closes)
    as_of_ms = btc[-1].start_time_ms

    if regime is GlobalRegime.BULL:
        return RegimeSpecialistDecision(
            regime=regime,
            as_of_ms=as_of_ms,
            bull_switch_active=bull_regime_switch_active(closes),
            bear_short_symbols=(),
        )
    if regime is GlobalRegime.TRANSITION:
        return RegimeSpecialistDecision(regime, as_of_ms, False, ())

    anchor_ms = _latest_completed_sunday_start(btc)
    if anchor_ms is None:
        return RegimeSpecialistDecision(regime, as_of_ms, False, ())

    returns: list[tuple[Decimal, str]] = []
    for raw_symbol in universe:
        symbol = raw_symbol.upper()
        daily = btc if symbol == "BTCUSDT" else _closed_daily(
            source.get_klines(symbol, "D", limit=320),
            now_ms=now_ms,
        )
        value = _return_28d_at_anchor(daily, anchor_ms)
        if value is not None:
            returns.append((value, symbol))
    returns.sort(key=lambda item: (item[0], item[1]))
    return RegimeSpecialistDecision(
        regime=regime,
        as_of_ms=as_of_ms,
        bull_switch_active=False,
        bear_short_symbols=tuple(symbol for _, symbol in returns[:3]),
    )


def filter_regime_specialist_candidates(
    candidates: tuple[DiscoveryResult, ...],
    decision: RegimeSpecialistDecision,
) -> tuple[DiscoveryResult, ...]:
    if decision.regime is GlobalRegime.TRANSITION:
        return ()
    if decision.regime is GlobalRegime.BULL:
        if not decision.bull_switch_active:
            return ()
        return tuple(
            candidate
            for candidate in candidates
            if candidate.symbol == "BTCUSDT" and candidate.direction is TradeDirection.LONG
        )
    allowed = frozenset(decision.bear_short_symbols)
    return tuple(
        candidate
        for candidate in candidates
        if candidate.symbol in allowed and candidate.direction is TradeDirection.SHORT
    )
