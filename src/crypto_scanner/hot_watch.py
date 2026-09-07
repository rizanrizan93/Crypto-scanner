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
MAX_HOT_CANDIDATES = 4
FAST_WATCH_MAX_ROUNDS = 4
FAST_WATCH_INTERVAL_SECONDS = 60.0
CANDIDATE_TTL_MS = 5 * 60_000
DEMO_ACQUISITION_REASON = "DEMO_CALIBRATION_ACQUISITION_PROMOTED"
_DEMO_ACQUISITION_MIN_SCORE = Decimal("60")
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


def _eligible_demo_watch(result: DiscoveryResult) -> bool:
    return (
        result.status is DiscoveryStatus.WATCH
        and result.direction in {TradeDirection.LONG, TradeDirection.SHORT}
        and result.ranking_score >= _DEMO_ACQUISITION_MIN_SCORE
        and result.evidence_coverage >= _DEMO_ACQUISITION_MIN_COVERAGE
    )


def _promote_demo_watch(result: DiscoveryResult) -> DiscoveryResult:
    return replace(
        result,
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
    """Capture 1m/3m trigger evidence without changing execution thresholds."""
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

    return {
        "move_1m_bps": str(one_move) if one_move is not None else None,
        "move_3m_bps": str(three_move) if three_move is not None else None,
        "one_minute_displacement": displacement,
        "reclaim_or_retest_1m": reclaim_or_retest,
        "spread_bps": str(ticker.spread_bps),
        "orderbook_imbalance": (
            str(orderbook_imbalance) if orderbook_imbalance is not None else None
        ),
        "taker_pressure": str(taker_pressure) if taker_pressure is not None else None,
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
