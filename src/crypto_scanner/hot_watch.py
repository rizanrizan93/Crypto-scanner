from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum

from crypto_scanner.discovery import DiscoveryResult, DiscoveryStatus, TradeDirection
from crypto_scanner.fast_lane import ReadinessDecision
from crypto_scanner.persistence import SupabasePersistenceConfig, SupabaseRestClient

HOT_WATCH_STATE_VERSION = 1
HOT_WATCH_SCHEMA = "crypto-hot-watch-v1"
MAX_HOT_CANDIDATES = 4
FAST_WATCH_MAX_ROUNDS = 4
FAST_WATCH_INTERVAL_SECONDS = 60.0
CANDIDATE_TTL_MS = 5 * 60_000


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


def select_hot_candidates(
    results: tuple[DiscoveryResult, ...],
    *,
    limit: int = MAX_HOT_CANDIDATES,
) -> tuple[DiscoveryResult, ...]:
    if not 1 <= limit <= MAX_HOT_CANDIDATES:
        raise ValueError(f"hot candidate limit must be between 1 and {MAX_HOT_CANDIDATES}")
    candidates = tuple(
        result
        for result in results
        if result.status is DiscoveryStatus.CANDIDATE
        and result.direction in {TradeDirection.LONG, TradeDirection.SHORT}
    )
    return tuple(
        sorted(
            candidates,
            key=lambda result: (-result.ranking_score, result.symbol),
        )[:limit]
    )


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


class HotWatchStore:
    """Durable per-run Fast Watch evidence using the existing runtime_state table."""

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
        key = f"hotwatch:{observation.run_id}:{observation.symbol}"
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
