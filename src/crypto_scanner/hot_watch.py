from __future__ import annotations

import os
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from enum import StrEnum

from crypto_scanner.bybit.models import Candle, TickerSnapshot
from crypto_scanner.discovery import DiscoveryResult, DiscoveryStatus, TradeDirection
from crypto_scanner.fast_lane import ReadinessDecision
from crypto_scanner.persistence import SupabasePersistenceConfig, SupabaseRestClient
from crypto_scanner.technical import closed_candles

HOT_WATCH_STATE_VERSION = 1
HOT_WATCH_SCHEMA = "crypto-hot-watch-v1"
RESEARCH_SCHEMA = "crypto-factor-research-v1"
RESEARCH_STRATEGY_ID = "EVIDENCE_WEIGHTED_TREND_ORDERFLOW_V1"
MAX_HOT_CANDIDATES = 4
FAST_WATCH_MAX_ROUNDS = 4
FAST_WATCH_INTERVAL_SECONDS = 60.0
CANDIDATE_TTL_MS = 5 * 60_000
DEMO_ACQUISITION_REASON = "DEMO_CALIBRATION_ACQUISITION_PROMOTED"
_DEMO_ACQUISITION_SCORE_FLOOR = Decimal("50")
_DEMO_ACQUISITION_MIN_SEPARATION = Decimal("5")
_DEMO_ACQUISITION_MIN_COVERAGE = Decimal("0.72")


class HotWatchStatus(StrEnum):
    HOT_CANDIDATE = "HOT_CANDIDATE"
    WATCHING = "WATCHING"
    EXECUTION_READY = "EXECUTION_READY"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class HotWatchObservation:
    run_id: str
    symbol: str
    direction: TradeDirection
    discovery_score: str
    round_index: int
    observed_at_ms: int
    candidate_timestamp_ms: int
    status: HotWatchStatus
    reasons: tuple[str, ...]
    signal_id: str | None = None
    telemetry: dict[str, object] | None = None

    def payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["direction"] = self.direction.value
        payload["status"] = self.status.value
        payload["schema"] = HOT_WATCH_SCHEMA
        return payload


_INVALIDATING_REASONS = frozenset(
    {
        "NOT_DISCOVERY_CANDIDATE",
        "DIRECTION_NOT_TRADABLE",
        "INVALID_CLOCK",
        "INVALID_QUOTE",
        "ORDERBOOK_IMBALANCE_INVALID",
        "TAKER_PRESSURE_INVALID",
        "3M_DATA_INSUFFICIENT",
        "5M_DATA_INSUFFICIENT",
        "CHASE_TOO_FAR",
        "RR_TOO_LOW",
    }
)


def _demo_acquisition_enabled() -> bool:
    return os.getenv("CRYPTO_SCANNER_TESTNET_EXECUTION", "").strip().upper() == "ENABLED"


def _demo_acquisition_direction(result: DiscoveryResult) -> TradeDirection:
    if (
        result.long_score <= _DEMO_ACQUISITION_SCORE_FLOOR
        and result.short_score <= _DEMO_ACQUISITION_SCORE_FLOOR
    ):
        return TradeDirection.NEUTRAL
    separation = abs(result.long_score - result.short_score)
    if separation < _DEMO_ACQUISITION_MIN_SEPARATION:
        return TradeDirection.NEUTRAL
    if result.long_score > result.short_score:
        return TradeDirection.LONG
    return TradeDirection.SHORT


def _eligible_demo_watch(result: DiscoveryResult) -> bool:
    direction = _demo_acquisition_direction(result)
    selected_score = (
        result.long_score if direction is TradeDirection.LONG else result.short_score
    )
    return (
        result.status is DiscoveryStatus.WATCH
        and direction in {TradeDirection.LONG, TradeDirection.SHORT}
        and selected_score > _DEMO_ACQUISITION_SCORE_FLOOR
        and result.evidence_coverage >= _DEMO_ACQUISITION_MIN_COVERAGE
    )


def _promote_demo_watch(result: DiscoveryResult) -> DiscoveryResult:
    direction = _demo_acquisition_direction(result)
    return replace(
        result,
        direction=direction,
        status=DiscoveryStatus.CANDIDATE,
        reasons=tuple(dict.fromkeys((*result.reasons, DEMO_ACQUISITION_REASON))),
    )


def select_hot_candidates(
    results: tuple[DiscoveryResult, ...],
    *,
    limit: int = MAX_HOT_CANDIDATES,
) -> tuple[DiscoveryResult, ...]:
    if not 1 <= limit <= MAX_HOT_CANDIDATES:
        raise ValueError(f"hot candidate limit must be between 1 and {MAX_HOT_CANDIDATES}")

    strict = sorted(
        (
            result
            for result in results
            if result.status is DiscoveryStatus.CANDIDATE
            and result.direction in {TradeDirection.LONG, TradeDirection.SHORT}
        ),
        key=lambda result: (-result.ranking_score, result.symbol),
    )
    selected = list(strict[:limit])
    if len(selected) >= limit or not _demo_acquisition_enabled():
        return tuple(selected)

    acquisition_pool = sorted(
        (_promote_demo_watch(result) for result in results if _eligible_demo_watch(result)),
        key=lambda result: (-result.ranking_score, result.symbol),
    )
    selected.extend(acquisition_pool[: limit - len(selected)])
    return tuple(selected)


def classify_hot_watch(
    decision: ReadinessDecision,
    *,
    candidate_timestamp_ms: int,
    now_ms: int,
) -> HotWatchStatus:
    age = now_ms - candidate_timestamp_ms
    if age < 0 or age > CANDIDATE_TTL_MS or "STALE_CANDIDATE" in decision.reasons:
        return HotWatchStatus.EXPIRED
    if decision.execution_ready:
        return HotWatchStatus.EXECUTION_READY
    for reason in decision.reasons:
        root = reason.split(":", 1)[0]
        if root in _INVALIDATING_REASONS or root == "GEOMETRY_INVALID":
            return HotWatchStatus.INVALIDATED
    return HotWatchStatus.WATCHING


def _closed_pair(
    candles: tuple[Candle, ...],
    *,
    interval_minutes: int,
    now_ms: int,
) -> tuple[Candle, Candle] | None:
    try:
        filtered = closed_candles(
            candles,
            interval_minutes=interval_minutes,
            now_ms=now_ms,
        )
    except ValueError:
        return None
    if len(filtered) < 2:
        return None
    return filtered[-2], filtered[-1]


def _move_bps(pair: tuple[Candle, Candle] | None) -> Decimal | None:
    if pair is None or pair[0].close <= 0:
        return None
    return (pair[1].close - pair[0].close) / pair[0].close * Decimal("10000")


def _frame_research_snapshot(candidate: DiscoveryResult) -> dict[str, object]:
    if candidate.direction not in {TradeDirection.LONG, TradeDirection.SHORT}:
        return {}
    bullish = candidate.direction is TradeDirection.LONG
    expected_bias = "BULLISH" if bullish else "BEARISH"
    frames: dict[str, object] = {}
    for frame in candidate.frames:
        structure_aligned = frame.structure.bias.value == expected_bias
        ema_aligned = (
            frame.last_price > frame.regime.ema20 > frame.regime.ema50
            if bullish
            else frame.last_price < frame.regime.ema20 < frame.regime.ema50
        )
        signed_momentum = frame.regime.momentum10 if bullish else -frame.regime.momentum10
        momentum_aligned = signed_momentum > 0
        momentum_to_atr = (
            signed_momentum / frame.regime.atr_pct
            if frame.regime.atr_pct > 0
            else None
        )
        frames[frame.timeframe] = {
            "last_price": str(frame.last_price),
            "rsi14": str(frame.rsi14),
            "structure_bias": frame.structure.bias.value,
            "structure_event": frame.structure.event.value,
            "last_swing_high": str(frame.structure.last_swing_high),
            "last_swing_low": str(frame.structure.last_swing_low),
            "regime": frame.regime.regime.value,
            "adx14": str(frame.regime.adx14),
            "atr14": str(frame.regime.atr14),
            "atr_pct": str(frame.regime.atr_pct),
            "atr_expansion": str(frame.regime.atr_expansion),
            "ema20": str(frame.regime.ema20),
            "ema50": str(frame.regime.ema50),
            "momentum10": str(frame.regime.momentum10),
            "direction_signed_momentum10": str(signed_momentum),
            "direction_signed_momentum_to_atr": (
                str(momentum_to_atr) if momentum_to_atr is not None else None
            ),
            "structure_aligned": structure_aligned,
            "ema_aligned": ema_aligned,
            "momentum_aligned": momentum_aligned,
            "alignment_count": sum((structure_aligned, ema_aligned, momentum_aligned)),
        }
    return frames


def build_hot_watch_telemetry(
    candidate: DiscoveryResult,
    *,
    candles_1m: tuple[Candle, ...],
    candles_3m: tuple[Candle, ...],
    ticker: TickerSnapshot,
    orderbook_imbalance: Decimal | None,
    taker_pressure: Decimal | None,
    now_ms: int,
) -> dict[str, object]:
    """Capture trigger and research evidence without changing execution thresholds."""
    one_pair = _closed_pair(candles_1m, interval_minutes=1, now_ms=now_ms)
    three_pair = _closed_pair(candles_3m, interval_minutes=3, now_ms=now_ms)
    one_move = _move_bps(one_pair)
    three_move = _move_bps(three_pair)

    displacement = False
    reclaim_or_retest = False
    if one_pair is not None:
        previous, latest = one_pair
        candle_range = latest.high - latest.low
        body = abs(latest.close - latest.open)
        signed_body_aligned = (
            candidate.direction is TradeDirection.LONG and latest.close > latest.open
        ) or (
            candidate.direction is TradeDirection.SHORT and latest.close < latest.open
        )
        displacement = (
            candle_range > 0
            and body / candle_range >= Decimal("0.60")
            and signed_body_aligned
        )
        if candidate.direction is TradeDirection.LONG:
            reclaim_or_retest = latest.low <= previous.high and latest.close > previous.high
        elif candidate.direction is TradeDirection.SHORT:
            reclaim_or_retest = latest.high >= previous.low and latest.close < previous.low

    reference_price: Decimal | None = None
    for frame in candidate.frames:
        if frame.timeframe == "5":
            reference_price = frame.last_price
            break
    if reference_price is None and three_pair is not None:
        reference_price = three_pair[1].close
    proximity_bps = None
    if reference_price is not None and reference_price > 0:
        proximity_bps = abs(ticker.mid_price - reference_price) / reference_price * Decimal("10000")

    direction_sign = Decimal(1) if candidate.direction is TradeDirection.LONG else Decimal(-1)
    signed_book = (
        orderbook_imbalance * direction_sign if orderbook_imbalance is not None else None
    )
    signed_taker = taker_pressure * direction_sign if taker_pressure is not None else None
    signed_move_1m = one_move * direction_sign if one_move is not None else None
    signed_move_3m = three_move * direction_sign if three_move is not None else None
    funding = ticker.funding_rate
    signed_funding = funding * direction_sign if funding is not None else None

    return {
        "research_schema": RESEARCH_SCHEMA,
        "strategy_id": RESEARCH_STRATEGY_ID,
        "captured_at_ms": now_ms,
        "direction": candidate.direction.value,
        "discovery_score": str(candidate.ranking_score),
        "long_score": str(candidate.long_score),
        "short_score": str(candidate.short_score),
        "score_separation": str(abs(candidate.long_score - candidate.short_score)),
        "evidence_coverage": str(candidate.evidence_coverage),
        "context_bias": candidate.context_bias.value,
        "context_adjustment_long": str(candidate.context_adjustment_long),
        "context_adjustment_short": str(candidate.context_adjustment_short),
        "frames": _frame_research_snapshot(candidate),
        "move_1m_bps": str(one_move) if one_move is not None else None,
        "move_3m_bps": str(three_move) if three_move is not None else None,
        "direction_signed_move_1m_bps": (
            str(signed_move_1m) if signed_move_1m is not None else None
        ),
        "direction_signed_move_3m_bps": (
            str(signed_move_3m) if signed_move_3m is not None else None
        ),
        "one_minute_displacement": displacement,
        "reclaim_or_retest_1m": reclaim_or_retest,
        "last_price": str(ticker.last_price),
        "mark_price": str(ticker.mark_price),
        "index_price": str(ticker.index_price),
        "spread_bps": str(ticker.spread_bps),
        "volume_24h": str(ticker.volume_24h) if ticker.volume_24h is not None else None,
        "turnover_24h": str(ticker.turnover_24h) if ticker.turnover_24h is not None else None,
        "open_interest": str(ticker.open_interest) if ticker.open_interest is not None else None,
        "funding_rate": str(funding) if funding is not None else None,
        "direction_signed_funding_rate": (
            str(signed_funding) if signed_funding is not None else None
        ),
        "orderbook_imbalance": (
            str(orderbook_imbalance) if orderbook_imbalance is not None else None
        ),
        "direction_signed_orderbook_imbalance": (
            str(signed_book) if signed_book is not None else None
        ),
        "taker_pressure": str(taker_pressure) if taker_pressure is not None else None,
        "direction_signed_taker_pressure": (
            str(signed_taker) if signed_taker is not None else None
        ),
        "distance_to_discovery_reference_bps": (
            str(proximity_bps) if proximity_bps is not None else None
        ),
    }


class HotWatchStore:
    """Durable per-round Fast Watch evidence using the existing runtime_state table."""

    def __init__(self, config: SupabasePersistenceConfig) -> None:
        self._rest = SupabaseRestClient(config)

    def close(self) -> None:
        self._rest.close()

    def __enter__(self) -> HotWatchStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def save(self, observation: HotWatchObservation) -> None:
        if observation.round_index < 0:
            raise ValueError("hot watch round_index cannot be negative")
        key = (
            f"hotwatch:{observation.run_id}:{observation.symbol}:"
            f"{observation.round_index}"
        )
        self._rest.upsert(
            "runtime_state",
            (
                {
                    "state_key": key,
                    "version": HOT_WATCH_STATE_VERSION,
                    "state": observation.payload(),
                    "updated_at_ms": observation.observed_at_ms,
                },
            ),
            on_conflict=("state_key",),
        )
