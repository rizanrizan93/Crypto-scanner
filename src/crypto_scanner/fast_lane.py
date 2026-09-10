from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from crypto_scanner.discovery import DiscoveryResult, DiscoveryStatus, TradeDirection
from crypto_scanner.market_models import Candle, InstrumentInfo, TickerSnapshot
from crypto_scanner.signal_geometry import (
    GeometryError,
    SignalGeometry,
    build_demo_technical_scalp_geometry,
    build_signal_geometry,
)
from crypto_scanner.strategy_params import DEFAULT_STRATEGY_PARAMETERS, StrategyParameters
from crypto_scanner.structure import StructuralBias
from crypto_scanner.technical import closed_candles

_ORDERBOOK_ALIGNMENT_THRESHOLD = Decimal("0.05")
_TAKER_PRESSURE_ALIGNMENT_THRESHOLD = Decimal("0.03")
_BASE_SPREAD_LIMIT_BPS = Decimal("5")
_DEMO_ACQUISITION_REASON = "DEMO_CALIBRATION_ACQUISITION_PROMOTED"
_DEMO_TEMPORAL_CONFIRMATION_REASON = "DEMO_TEMPORAL_MICROSTRUCTURE_2_OF_3"
_DEMO_TECHNICAL_FIRST_REASON = "DEMO_TECHNICAL_FIRST_15M_MICRO_SOFT_CONFIRMATION"
_DEMO_TECHNICAL_SCALP_GEOMETRY_REASON = "DEMO_TECHNICAL_15M_SCALP_GEOMETRY"
_DEMO_TEMPORAL_WINDOW = 3
_DEMO_TEMPORAL_REQUIRED_SUPPORT = 2
_DEMO_TECHNICAL_SCORE_FLOOR = Decimal("50")
_DEMO_TECHNICAL_MIN_COVERAGE = Decimal("0.72")
_DEMO_STRONGLY_ADVERSE_MICRO_THRESHOLD = Decimal("0.20")
_DEMO_MICRO_HISTORY: dict[
    tuple[str, int, TradeDirection],
    list[tuple[int, bool, bool]],
] = {}


class ReadinessStatus(StrEnum):
    EXECUTION_READY = "EXECUTION_READY"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class FastLaneEvidence:
    quote_timestamp_ms: int
    candidate_timestamp_ms: int
    orderbook_timestamp_ms: int
    orderbook_imbalance: Decimal | None
    taker_pressure: Decimal | None
    exchange_healthy: bool = True
    orderbook_healthy: bool = True


@dataclass(frozen=True, slots=True)
class ReadinessDecision:
    symbol: str
    status: ReadinessStatus
    geometry: SignalGeometry | None
    reasons: tuple[str, ...]

    @property
    def execution_ready(self) -> bool:
        return self.status is ReadinessStatus.EXECUTION_READY


def effective_spread_limit_bps(
    ticker: TickerSnapshot,
    instrument: InstrumentInfo,
) -> Decimal:
    """Keep the 5 bps guard, but never demand a spread tighter than one venue tick."""
    mid_price = ticker.mid_price
    if mid_price <= 0:
        raise ValueError("mid price must be positive")
    if instrument.tick_size <= 0:
        raise ValueError("tick size must be positive")
    one_tick_bps = instrument.tick_size / mid_price * Decimal("10000")
    return max(_BASE_SPREAD_LIMIT_BPS, one_tick_bps)


def _aligned_microstructure(
    direction: TradeDirection,
    value: Decimal,
    threshold: Decimal,
) -> bool:
    if direction is TradeDirection.LONG:
        return value >= threshold
    if direction is TradeDirection.SHORT:
        return value <= -threshold
    return False


def _signed_for_direction(direction: TradeDirection, value: Decimal) -> Decimal:
    if direction is TradeDirection.LONG:
        return value
    if direction is TradeDirection.SHORT:
        return -value
    return Decimal(0)


def _technical_15m_confirmed(candidate: DiscoveryResult) -> bool:
    if candidate.direction not in {TradeDirection.LONG, TradeDirection.SHORT}:
        return False
    frame = next((item for item in candidate.frames if item.timeframe == "15"), None)
    if frame is None:
        return False

    bullish = candidate.direction is TradeDirection.LONG
    structure_aligned = (
        bullish and frame.structure.bias is StructuralBias.BULLISH
    ) or (
        not bullish and frame.structure.bias is StructuralBias.BEARISH
    )
    ema_aligned = (
        bullish and frame.last_price > frame.regime.ema20 > frame.regime.ema50
    ) or (
        not bullish and frame.last_price < frame.regime.ema20 < frame.regime.ema50
    )
    momentum_aligned = (
        bullish and frame.regime.momentum10 > 0
    ) or (
        not bullish and frame.regime.momentum10 < 0
    )
    return sum((structure_aligned, ema_aligned, momentum_aligned)) >= 2


def _demo_technical_first_enabled(candidate: DiscoveryResult) -> bool:
    execution_enabled = (
        os.getenv("CRYPTO_SCANNER_TESTNET_EXECUTION", "").strip().upper() == "ENABLED"
    )
    return (
        execution_enabled
        and candidate.status is DiscoveryStatus.CANDIDATE
        and candidate.direction in {TradeDirection.LONG, TradeDirection.SHORT}
        and candidate.ranking_score > _DEMO_TECHNICAL_SCORE_FLOOR
        and candidate.evidence_coverage >= _DEMO_TECHNICAL_MIN_COVERAGE
        and _technical_15m_confirmed(candidate)
    )


def _strongly_adverse_microstructure(
    candidate: DiscoveryResult,
    evidence: FastLaneEvidence,
) -> bool:
    if candidate.direction not in {TradeDirection.LONG, TradeDirection.SHORT}:
        return False
    if not _micro_value_valid(evidence.orderbook_imbalance) or not _micro_value_valid(
        evidence.taker_pressure
    ):
        return False
    assert evidence.orderbook_imbalance is not None
    assert evidence.taker_pressure is not None
    orderbook_signed = _signed_for_direction(
        candidate.direction,
        evidence.orderbook_imbalance,
    )
    taker_signed = _signed_for_direction(candidate.direction, evidence.taker_pressure)
    return (
        orderbook_signed <= -_DEMO_STRONGLY_ADVERSE_MICRO_THRESHOLD
        and taker_signed <= -_DEMO_STRONGLY_ADVERSE_MICRO_THRESHOLD
    )


def _demo_temporal_enabled(candidate: DiscoveryResult) -> bool:
    execution_enabled = (
        os.getenv("CRYPTO_SCANNER_TESTNET_EXECUTION", "").strip().upper() == "ENABLED"
    )
    return execution_enabled and _DEMO_ACQUISITION_REASON in candidate.reasons


def _micro_value_valid(value: Decimal | None) -> bool:
    return value is not None and Decimal(-1) <= value <= Decimal(1)


def _demo_temporal_microstructure_confirmed(
    candidate: DiscoveryResult,
    evidence: FastLaneEvidence,
    *,
    book_age: int,
) -> bool:
    """Confirm promoted Demo candidates from a bounded 3-snapshot micro window.

    A valid snapshot is supportive when at least one of orderbook imbalance or
    taker pressure is aligned with the candidate direction. Two of the latest
    three valid snapshots must be supportive, both evidence types must have
    aligned at least once in that window, and the current snapshot must retain
    at least one aligned signal. Missing, invalid, or stale evidence never
    contributes to confirmation.
    """
    if not _demo_temporal_enabled(candidate):
        return False
    if candidate.direction not in {TradeDirection.LONG, TradeDirection.SHORT}:
        return False
    if book_age < 0 or book_age > 2_000:
        return False
    if not _micro_value_valid(evidence.orderbook_imbalance) or not _micro_value_valid(
        evidence.taker_pressure
    ):
        return False

    assert evidence.orderbook_imbalance is not None
    assert evidence.taker_pressure is not None
    orderbook_aligned = _aligned_microstructure(
        candidate.direction,
        evidence.orderbook_imbalance,
        _ORDERBOOK_ALIGNMENT_THRESHOLD,
    )
    taker_aligned = _aligned_microstructure(
        candidate.direction,
        evidence.taker_pressure,
        _TAKER_PRESSURE_ALIGNMENT_THRESHOLD,
    )
    current_supportive = orderbook_aligned or taker_aligned

    key = (candidate.symbol, evidence.candidate_timestamp_ms, candidate.direction)
    history = _DEMO_MICRO_HISTORY.setdefault(key, [])
    observation = (
        evidence.orderbook_timestamp_ms,
        orderbook_aligned,
        taker_aligned,
    )
    if not history or history[-1][0] != evidence.orderbook_timestamp_ms:
        history.append(observation)
        if len(history) > _DEMO_TEMPORAL_WINDOW:
            del history[:-_DEMO_TEMPORAL_WINDOW]

    if len(history) < _DEMO_TEMPORAL_WINDOW:
        return False
    supportive_count = sum(orderbook or taker for _, orderbook, taker in history)
    orderbook_seen = any(orderbook for _, orderbook, _ in history)
    taker_seen = any(taker for _, _, taker in history)
    return (
        supportive_count >= _DEMO_TEMPORAL_REQUIRED_SUPPORT
        and orderbook_seen
        and taker_seen
        and current_supportive
    )


def evaluate_execution_readiness(
    candidate: DiscoveryResult,
    *,
    candles_3m: tuple[Candle, ...],
    candles_5m: tuple[Candle, ...],
    ticker: TickerSnapshot,
    instrument: InstrumentInfo,
    evidence: FastLaneEvidence,
    now_ms: int,
    strategy: StrategyParameters | None = None,
) -> ReadinessDecision:
    strategy = strategy or DEFAULT_STRATEGY_PARAMETERS
    strategy.validate()
    reasons: list[str] = []

    if candidate.status is not DiscoveryStatus.CANDIDATE:
        reasons.append("NOT_DISCOVERY_CANDIDATE")
    if candidate.direction not in {TradeDirection.LONG, TradeDirection.SHORT}:
        reasons.append("DIRECTION_NOT_TRADABLE")
    if now_ms < 0:
        reasons.append("INVALID_CLOCK")
    if not evidence.exchange_healthy:
        reasons.append("EXCHANGE_UNHEALTHY")
    if not evidence.orderbook_healthy:
        reasons.append("ORDERBOOK_UNHEALTHY")

    quote_age = now_ms - evidence.quote_timestamp_ms
    candidate_age = now_ms - evidence.candidate_timestamp_ms
    book_age = now_ms - evidence.orderbook_timestamp_ms
    if quote_age < 0 or quote_age > 2_000:
        reasons.append("STALE_QUOTE")
    if book_age < 0 or book_age > 2_000:
        reasons.append("STALE_ORDERBOOK")
    if candidate_age < 0 or candidate_age > 5 * 60_000:
        reasons.append("STALE_CANDIDATE")

    try:
        spread_bps = ticker.spread_bps
        spread_limit_bps = effective_spread_limit_bps(ticker, instrument)
    except ValueError:
        spread_bps = Decimal("999999")
        spread_limit_bps = _BASE_SPREAD_LIMIT_BPS
        reasons.append("INVALID_QUOTE")
    if spread_bps > spread_limit_bps:
        reasons.append("SPREAD_TOO_WIDE")

    temporal_micro_confirmed = _demo_temporal_microstructure_confirmed(
        candidate,
        evidence,
        book_age=book_age,
    )
    demo_technical_first = _demo_technical_first_enabled(candidate)
    strongly_adverse_micro = _strongly_adverse_microstructure(candidate, evidence)
    orderbook_aligned = False
    taker_aligned = False

    if evidence.orderbook_imbalance is None:
        reasons.append("ORDERBOOK_IMBALANCE_MISSING")
    elif not Decimal(-1) <= evidence.orderbook_imbalance <= Decimal(1):
        reasons.append("ORDERBOOK_IMBALANCE_INVALID")
    elif candidate.direction in {TradeDirection.LONG, TradeDirection.SHORT}:
        orderbook_aligned = _aligned_microstructure(
            candidate.direction,
            evidence.orderbook_imbalance,
            _ORDERBOOK_ALIGNMENT_THRESHOLD,
        )
        if (
            not orderbook_aligned
            and not temporal_micro_confirmed
            and not demo_technical_first
        ):
            reasons.append("ORDERBOOK_NOT_ALIGNED")

    if evidence.taker_pressure is None:
        reasons.append("TAKER_PRESSURE_MISSING")
    elif not Decimal(-1) <= evidence.taker_pressure <= Decimal(1):
        reasons.append("TAKER_PRESSURE_INVALID")
    elif candidate.direction in {TradeDirection.LONG, TradeDirection.SHORT}:
        taker_aligned = _aligned_microstructure(
            candidate.direction,
            evidence.taker_pressure,
            _TAKER_PRESSURE_ALIGNMENT_THRESHOLD,
        )
        if not taker_aligned and not temporal_micro_confirmed and not demo_technical_first:
            reasons.append("TAKER_PRESSURE_NOT_ALIGNED")

    if demo_technical_first and strongly_adverse_micro:
        reasons.append("MICROSTRUCTURE_STRONGLY_ADVERSE")

    for interval_minutes, candles, label in (
        (3, candles_3m, "3M"),
        (5, candles_5m, "5M"),
    ):
        try:
            filtered = closed_candles(
                candles,
                interval_minutes=interval_minutes,
                now_ms=now_ms,
            )
        except ValueError:
            filtered = ()
        if len(filtered) < 100:
            reasons.append(f"{label}_DATA_INSUFFICIENT")
            continue
        interval_ms = interval_minutes * 60_000
        last_close_ms = filtered[-1].start_time_ms + interval_ms
        if now_ms - last_close_ms > interval_ms * 2:
            reasons.append(f"{label}_DATA_STALE")

    geometry: SignalGeometry | None = None
    used_demo_scalp_geometry = False
    if not reasons:
        try:
            geometry = build_signal_geometry(
                candidate,
                candles_3m=candles_3m,
                candles_5m=candles_5m,
                ticker=ticker,
                instrument=instrument,
                strategy=strategy,
            )
        except (GeometryError, ValueError) as primary_exc:
            if demo_technical_first:
                try:
                    geometry = build_demo_technical_scalp_geometry(
                        candidate,
                        candles_3m=candles_3m,
                        candles_5m=candles_5m,
                        ticker=ticker,
                        instrument=instrument,
                        strategy=strategy,
                    )
                    used_demo_scalp_geometry = True
                except (GeometryError, ValueError) as fallback_exc:
                    reasons.append(
                        "GEOMETRY_INVALID:"
                        f"primary={primary_exc}; demo_fallback={fallback_exc}"
                    )
            else:
                reasons.append(f"GEOMETRY_INVALID:{primary_exc}")

    if geometry is not None:
        if geometry.chase_atr > strategy.max_chase_atr:
            reasons.append("CHASE_TOO_FAR")
        if geometry.rr_tp1 < strategy.min_rr_tp1 or geometry.rr_tp2 < strategy.min_rr_tp2:
            reasons.append("RR_TOO_LOW")

    if reasons:
        return ReadinessDecision(
            symbol=candidate.symbol,
            status=ReadinessStatus.REJECTED,
            geometry=None,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    ready_reasons = ("ALL_HARD_GUARDS_PASSED",)
    if demo_technical_first and not (orderbook_aligned and taker_aligned):
        ready_reasons += (_DEMO_TECHNICAL_FIRST_REASON,)
    elif temporal_micro_confirmed and not (orderbook_aligned and taker_aligned):
        ready_reasons += (_DEMO_TEMPORAL_CONFIRMATION_REASON,)
    if used_demo_scalp_geometry:
        ready_reasons += (_DEMO_TECHNICAL_SCALP_GEOMETRY_REASON,)
    return ReadinessDecision(
        symbol=candidate.symbol,
        status=ReadinessStatus.EXECUTION_READY,
        geometry=geometry,
        reasons=ready_reasons,
    )
