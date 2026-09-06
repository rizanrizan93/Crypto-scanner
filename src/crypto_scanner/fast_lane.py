from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from crypto_scanner.bybit.models import Candle, InstrumentInfo, TickerSnapshot
from crypto_scanner.discovery import DiscoveryResult, DiscoveryStatus, TradeDirection
from crypto_scanner.signal_geometry import GeometryError, SignalGeometry, build_signal_geometry
from crypto_scanner.strategy_params import DEFAULT_STRATEGY_PARAMETERS, StrategyParameters
from crypto_scanner.technical import closed_candles


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
    except ValueError:
        spread_bps = Decimal("999999")
        reasons.append("INVALID_QUOTE")
    if spread_bps > Decimal("5"):
        reasons.append("SPREAD_TOO_WIDE")

    if evidence.orderbook_imbalance is None:
        reasons.append("ORDERBOOK_IMBALANCE_MISSING")
    elif not Decimal(-1) <= evidence.orderbook_imbalance <= Decimal(1):
        reasons.append("ORDERBOOK_IMBALANCE_INVALID")
    elif candidate.direction in {TradeDirection.LONG, TradeDirection.SHORT} and not (
        _aligned_microstructure(
            candidate.direction,
            evidence.orderbook_imbalance,
            Decimal("0.05"),
        )
    ):
        reasons.append("ORDERBOOK_NOT_ALIGNED")

    if evidence.taker_pressure is None:
        reasons.append("TAKER_PRESSURE_MISSING")
    elif not Decimal(-1) <= evidence.taker_pressure <= Decimal(1):
        reasons.append("TAKER_PRESSURE_INVALID")
    elif candidate.direction in {TradeDirection.LONG, TradeDirection.SHORT} and not (
        _aligned_microstructure(
            candidate.direction,
            evidence.taker_pressure,
            Decimal("0.03"),
        )
    ):
        reasons.append("TAKER_PRESSURE_NOT_ALIGNED")

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
        except (GeometryError, ValueError) as exc:
            reasons.append(f"GEOMETRY_INVALID:{exc}")

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

    return ReadinessDecision(
        symbol=candidate.symbol,
        status=ReadinessStatus.EXECUTION_READY,
        geometry=geometry,
        reasons=("ALL_HARD_GUARDS_PASSED",),
    )
