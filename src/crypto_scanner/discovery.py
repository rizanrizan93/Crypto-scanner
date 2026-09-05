from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from crypto_scanner.bybit.models import Candle
from crypto_scanner.regime import MarketRegime, RegimeState, classify_regime
from crypto_scanner.structure import (
    StructuralBias,
    StructureEvent,
    StructureState,
    analyze_structure,
)
from crypto_scanner.technical import rsi, validate_candles


class TradeDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class DiscoveryStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    WATCH = "WATCH"
    NO_TRADE = "NO_TRADE"


class MarketContextBias(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    MIXED = "MIXED"
    CHAOTIC = "CHAOTIC"


@dataclass(frozen=True, slots=True)
class CryptoNativeEvidence:
    spread_bps: Decimal
    funding_rate: Decimal | None = None
    open_interest_change: Decimal | None = None
    orderbook_imbalance: Decimal | None = None
    taker_pressure: Decimal | None = None

    def validate(self) -> None:
        if self.spread_bps < 0:
            raise ValueError("spread_bps cannot be negative")
        for name, value in (
            ("orderbook_imbalance", self.orderbook_imbalance),
            ("taker_pressure", self.taker_pressure),
        ):
            if value is not None and not Decimal(-1) <= value <= Decimal(1):
                raise ValueError(f"{name} must be between -1 and 1")

    @property
    def coverage(self) -> Decimal:
        available = 1 + sum(
            value is not None
            for value in (
                self.funding_rate,
                self.open_interest_change,
                self.orderbook_imbalance,
                self.taker_pressure,
            )
        )
        return Decimal(available) / Decimal(5)


@dataclass(frozen=True, slots=True)
class FrameAnalysis:
    timeframe: str
    last_price: Decimal
    rsi14: Decimal
    structure: StructureState
    regime: RegimeState


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    symbol: str
    direction: TradeDirection
    status: DiscoveryStatus
    base_long_score: Decimal
    base_short_score: Decimal
    long_score: Decimal
    short_score: Decimal
    evidence_coverage: Decimal
    frames: tuple[FrameAnalysis, ...]
    reasons: tuple[str, ...]
    context_bias: MarketContextBias = MarketContextBias.MIXED
    context_adjustment_long: Decimal = Decimal(0)
    context_adjustment_short: Decimal = Decimal(0)

    @property
    def ranking_score(self) -> Decimal:
        if self.direction is TradeDirection.LONG:
            return self.long_score
        if self.direction is TradeDirection.SHORT:
            return self.short_score
        return max(self.long_score, self.short_score)


_FRAME_WEIGHTS = {
    "5": Decimal("0.25"),
    "15": Decimal("0.40"),
    "60": Decimal("0.35"),
}
_MIN_CANDIDATE_COVERAGE = Decimal("0.72")


def analyze_frame(timeframe: str, candles: tuple[Candle, ...]) -> FrameAnalysis:
    if timeframe not in _FRAME_WEIGHTS:
        raise ValueError(f"unsupported discovery timeframe: {timeframe}")
    validate_candles(candles, min_count=100)
    structure = analyze_structure(candles, swing_window=2)
    regime = classify_regime(candles, structure)
    closes = tuple(candle.close for candle in candles)
    return FrameAnalysis(
        timeframe=timeframe,
        last_price=closes[-1],
        rsi14=rsi(closes, 14),
        structure=structure,
        regime=regime,
    )


def _score_frame(frame: FrameAnalysis, direction: TradeDirection) -> Decimal:
    bullish = direction is TradeDirection.LONG
    score = Decimal(0)

    aligned_structure = (
        bullish and frame.structure.bias is StructuralBias.BULLISH
    ) or (
        not bullish and frame.structure.bias is StructuralBias.BEARISH
    )
    if aligned_structure:
        score += Decimal(22)

    favorable_event = (
        bullish
        and frame.structure.event
        in {StructureEvent.BOS_BULLISH, StructureEvent.CHOCH_BULLISH}
    ) or (
        not bullish
        and frame.structure.event
        in {StructureEvent.BOS_BEARISH, StructureEvent.CHOCH_BEARISH}
    )
    if favorable_event:
        score += Decimal(8 if "CHOCH" in frame.structure.event.value else 5)

    ema_aligned = (
        bullish and frame.last_price > frame.regime.ema20 > frame.regime.ema50
    ) or (
        not bullish and frame.last_price < frame.regime.ema20 < frame.regime.ema50
    )
    if ema_aligned:
        score += Decimal(18)

    momentum_aligned = (
        bullish and frame.regime.momentum10 > 0
    ) or (
        not bullish and frame.regime.momentum10 < 0
    )
    if momentum_aligned:
        if frame.regime.atr_pct > 0:
            strength = min(
                abs(frame.regime.momentum10) / frame.regime.atr_pct,
                Decimal(2),
            )
            score += Decimal(6) + strength * Decimal(3)
        else:
            score += Decimal(6)

    rsi_aligned = (
        bullish and Decimal(50) <= frame.rsi14 <= Decimal(72)
    ) or (
        not bullish and Decimal(28) <= frame.rsi14 <= Decimal(50)
    )
    if rsi_aligned:
        score += Decimal(10)

    if aligned_structure and frame.regime.adx14 >= Decimal(20):
        score += Decimal(8)

    if frame.regime.regime is MarketRegime.TREND and aligned_structure:
        score += Decimal(10)
    elif frame.regime.regime is MarketRegime.EXPANSION and momentum_aligned:
        score += Decimal(8)
    elif frame.regime.regime is MarketRegime.RANGE:
        score += Decimal(2)

    return min(score, Decimal(100))


def _native_adjustment(
    evidence: CryptoNativeEvidence,
    direction: TradeDirection,
    *,
    technical_momentum_aligned: bool,
) -> Decimal:
    bullish = direction is TradeDirection.LONG
    adjustment = Decimal(0)

    if evidence.spread_bps <= Decimal(2):
        adjustment += Decimal(2)
    elif evidence.spread_bps >= Decimal(5):
        adjustment -= min(Decimal(8), evidence.spread_bps - Decimal(4))

    if evidence.open_interest_change is not None and technical_momentum_aligned:
        if evidence.open_interest_change >= Decimal("0.002"):
            adjustment += Decimal(4)
        elif evidence.open_interest_change <= Decimal("-0.01"):
            adjustment -= Decimal(3)

    if evidence.taker_pressure is not None:
        signed = evidence.taker_pressure if bullish else -evidence.taker_pressure
        if signed > Decimal("0.10"):
            adjustment += min(Decimal(5), signed * Decimal(10))
        elif signed < Decimal("-0.20"):
            adjustment -= min(Decimal(5), abs(signed) * Decimal(8))

    if evidence.orderbook_imbalance is not None:
        signed = evidence.orderbook_imbalance if bullish else -evidence.orderbook_imbalance
        if signed > Decimal("0.10"):
            adjustment += min(Decimal(3), signed * Decimal(6))
        elif signed < Decimal("-0.25"):
            adjustment -= min(Decimal(3), abs(signed) * Decimal(5))

    if evidence.funding_rate is not None:
        crowded = (
            bullish and evidence.funding_rate >= Decimal("0.0005")
        ) or (
            not bullish and evidence.funding_rate <= Decimal("-0.0005")
        )
        if crowded:
            adjustment -= Decimal(4)

    return adjustment


def _direction_from_scores(
    long_score: Decimal,
    short_score: Decimal,
) -> TradeDirection:
    minimum = Decimal(58)
    separation = Decimal(8)
    if long_score >= minimum and long_score - short_score >= separation:
        return TradeDirection.LONG
    if short_score >= minimum and short_score - long_score >= separation:
        return TradeDirection.SHORT
    return TradeDirection.NEUTRAL


def _status_for(
    direction: TradeDirection,
    score: Decimal,
    coverage: Decimal,
    frames: tuple[FrameAnalysis, ...],
    evidence: CryptoNativeEvidence,
) -> DiscoveryStatus:
    if evidence.spread_bps >= Decimal(10):
        return DiscoveryStatus.NO_TRADE
    higher_frames = tuple(frame for frame in frames if frame.timeframe in {"15", "60"})
    if any(frame.regime.regime is MarketRegime.CHAOTIC for frame in higher_frames):
        return DiscoveryStatus.NO_TRADE
    if direction is TradeDirection.NEUTRAL:
        return DiscoveryStatus.WATCH
    if score >= Decimal(65) and coverage >= _MIN_CANDIDATE_COVERAGE:
        return DiscoveryStatus.CANDIDATE
    return DiscoveryStatus.WATCH


def analyze_symbol(
    symbol: str,
    candles_by_timeframe: dict[str, tuple[Candle, ...]],
    native: CryptoNativeEvidence,
) -> DiscoveryResult:
    native.validate()
    if set(candles_by_timeframe) != set(_FRAME_WEIGHTS):
        raise ValueError("discovery requires exactly 5m, 15m, and 60m candle sets")

    frames = tuple(
        analyze_frame(timeframe, candles_by_timeframe[timeframe])
        for timeframe in ("5", "15", "60")
    )
    weighted_long = sum(
        (
            _score_frame(frame, TradeDirection.LONG) * _FRAME_WEIGHTS[frame.timeframe]
            for frame in frames
        ),
        Decimal(0),
    )
    weighted_short = sum(
        (
            _score_frame(frame, TradeDirection.SHORT) * _FRAME_WEIGHTS[frame.timeframe]
            for frame in frames
        ),
        Decimal(0),
    )

    aggregate_momentum = sum(
        (
            frame.regime.momentum10 * _FRAME_WEIGHTS[frame.timeframe]
            for frame in frames
        ),
        Decimal(0),
    )
    weighted_long += _native_adjustment(
        native,
        TradeDirection.LONG,
        technical_momentum_aligned=aggregate_momentum > 0,
    )
    weighted_short += _native_adjustment(
        native,
        TradeDirection.SHORT,
        technical_momentum_aligned=aggregate_momentum < 0,
    )
    long_score = min(Decimal(100), max(Decimal(0), weighted_long))
    short_score = min(Decimal(100), max(Decimal(0), weighted_short))

    coverage = Decimal("0.60") + native.coverage * Decimal("0.40")
    direction = _direction_from_scores(long_score, short_score)
    selected_score = long_score if direction is TradeDirection.LONG else short_score
    status = _status_for(direction, selected_score, coverage, frames, native)

    reasons: list[str] = []
    if native.spread_bps >= Decimal(10):
        reasons.append("SPREAD_TOO_WIDE")
    if any(
        frame.regime.regime is MarketRegime.CHAOTIC
        for frame in frames
        if frame.timeframe in {"15", "60"}
    ):
        reasons.append("HIGHER_TIMEFRAME_CHAOTIC")
    if direction is TradeDirection.NEUTRAL:
        reasons.append("DIRECTION_NOT_SEPARATED")
    if coverage < _MIN_CANDIDATE_COVERAGE:
        reasons.append("EVIDENCE_COVERAGE_LOW")
    if not reasons:
        reasons.append("DISCOVERY_EVIDENCE_ALIGNED")

    return DiscoveryResult(
        symbol=symbol.upper(),
        direction=direction,
        status=status,
        base_long_score=long_score,
        base_short_score=short_score,
        long_score=long_score,
        short_score=short_score,
        evidence_coverage=coverage,
        frames=frames,
        reasons=tuple(reasons),
    )


def derive_market_context(results: tuple[DiscoveryResult, ...]) -> MarketContextBias:
    by_symbol = {result.symbol: result for result in results}
    btc = by_symbol.get("BTCUSDT")
    eth = by_symbol.get("ETHUSDT")
    if btc is None or eth is None:
        return MarketContextBias.MIXED
    if any(
        frame.regime.regime is MarketRegime.CHAOTIC
        for result in (btc, eth)
        for frame in result.frames
        if frame.timeframe in {"15", "60"}
    ):
        return MarketContextBias.CHAOTIC
    if (
        btc.direction is TradeDirection.LONG
        and eth.direction is TradeDirection.LONG
    ):
        return MarketContextBias.BULLISH
    if (
        btc.direction is TradeDirection.SHORT
        and eth.direction is TradeDirection.SHORT
    ):
        return MarketContextBias.BEARISH
    return MarketContextBias.MIXED


def apply_market_context(
    results: tuple[DiscoveryResult, ...],
) -> tuple[DiscoveryResult, ...]:
    context = derive_market_context(results)
    adjusted: list[DiscoveryResult] = []
    for result in results:
        long_adjustment = Decimal(0)
        short_adjustment = Decimal(0)
        if result.symbol not in {"BTCUSDT", "ETHUSDT"}:
            if context is MarketContextBias.BULLISH:
                long_adjustment = Decimal(4)
                short_adjustment = Decimal(-8)
            elif context is MarketContextBias.BEARISH:
                long_adjustment = Decimal(-8)
                short_adjustment = Decimal(4)
            elif context is MarketContextBias.CHAOTIC:
                long_adjustment = Decimal(-10)
                short_adjustment = Decimal(-10)

        long_score = min(
            Decimal(100), max(Decimal(0), result.base_long_score + long_adjustment)
        )
        short_score = min(
            Decimal(100), max(Decimal(0), result.base_short_score + short_adjustment)
        )
        direction = _direction_from_scores(long_score, short_score)
        selected_score = long_score if direction is TradeDirection.LONG else short_score

        if (
            context is MarketContextBias.CHAOTIC
            and result.symbol not in {"BTCUSDT", "ETHUSDT"}
        ):
            status = DiscoveryStatus.NO_TRADE
            reasons = tuple(dict.fromkeys((*result.reasons, "BTC_ETH_CONTEXT_CHAOTIC")))
        else:
            if result.status is DiscoveryStatus.NO_TRADE:
                status = DiscoveryStatus.NO_TRADE
            elif direction is TradeDirection.NEUTRAL:
                status = DiscoveryStatus.WATCH
            elif (
                selected_score >= Decimal(65)
                and result.evidence_coverage >= _MIN_CANDIDATE_COVERAGE
            ):
                status = DiscoveryStatus.CANDIDATE
            else:
                status = DiscoveryStatus.WATCH
            reasons = result.reasons

        adjusted.append(
            replace(
                result,
                direction=direction,
                status=status,
                long_score=long_score,
                short_score=short_score,
                context_bias=context,
                context_adjustment_long=long_adjustment,
                context_adjustment_short=short_adjustment,
                reasons=reasons,
            )
        )

    return tuple(
        sorted(
            adjusted,
            key=lambda item: (
                item.status is DiscoveryStatus.CANDIDATE,
                item.ranking_score,
            ),
            reverse=True,
        )
    )
