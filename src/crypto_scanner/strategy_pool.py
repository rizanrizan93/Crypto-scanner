from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

import httpx

from crypto_scanner.funding_oi_demo import FUNDING_OI_STRATEGY_ID
from crypto_scanner.funding_oi_demo import SLEEVE_STOP_RISK_BUDGET as FUNDING_OI_RISK
from crypto_scanner.persistence import (
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseRestClient,
    read_with_retry,
)
from crypto_scanner.regime_specialist_demo import (
    BULL_RISK_FRACTION,
    REGIME_SPECIALIST_STRATEGY_ID,
)
from crypto_scanner.volatility_breakout_demo import (
    SLEEVE_STOP_RISK_BUDGET as VOL_BREAKOUT_RISK,
)
from crypto_scanner.volatility_breakout_demo import VOLATILITY_BREAKOUT_STRATEGY_ID

POOL_STATE_KEY = "multi_strategy_demo_pool_v1"
POOL_SCHEMA_VERSION = "multi-strategy-demo-pool-v1"
PARALLEL_FORWARD_DEMO_STAGE = "FORWARD_DEMO_PARALLEL"
REGIME_FORWARD_DEMO_STAGE = "FORWARD_DEMO"


class DemoPoolStatus(StrEnum):
    FORWARD_DEMO_ACTIVE = "FORWARD_DEMO_ACTIVE"
    DEMO_VALIDATED = "DEMO_VALIDATED"
    DEMO_ROLLED_BACK = "DEMO_ROLLED_BACK"
    DEMO_QUARANTINED = "DEMO_QUARANTINED"

    @property
    def execution_authorized(self) -> bool:
        return self in {self.FORWARD_DEMO_ACTIVE, self.DEMO_VALIDATED}


@dataclass(frozen=True, slots=True)
class StrategyPoolDefinition:
    strategy_id: str
    display_name: str
    evidence_stage_filter: str
    risk_budget: Decimal
    research_source: str


@dataclass(frozen=True, slots=True)
class StrategyPoolEntry:
    strategy_id: str
    display_name: str
    evidence_stage_filter: str
    status: DemoPoolStatus
    risk_budget: Decimal
    research_source: str
    first_seen_ms: int
    last_evaluated_ms: int | None
    rank: int | None
    decision: str | None
    reasons: tuple[str, ...]
    metrics: dict[str, object]
    evidence: dict[str, object]
    live_trading_locked: bool = True

    @property
    def execution_authorized(self) -> bool:
        return self.status.execution_authorized and self.live_trading_locked

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "display_name": self.display_name,
            "evidence_stage_filter": self.evidence_stage_filter,
            "status": self.status.value,
            "risk_budget": str(self.risk_budget),
            "research_source": self.research_source,
            "first_seen_ms": self.first_seen_ms,
            "last_evaluated_ms": self.last_evaluated_ms,
            "rank": self.rank,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "metrics": self.metrics,
            "evidence": self.evidence,
            "live_trading_locked": self.live_trading_locked,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> StrategyPoolEntry:
        try:
            reasons_raw = raw.get("reasons", [])
            metrics_raw = raw.get("metrics", {})
            evidence_raw = raw.get("evidence", {})
            if not isinstance(reasons_raw, list):
                raise TypeError("reasons")
            if not isinstance(metrics_raw, dict) or not isinstance(evidence_raw, dict):
                raise TypeError("metrics/evidence")
            return cls(
                strategy_id=str(raw["strategy_id"]),
                display_name=str(raw["display_name"]),
                evidence_stage_filter=str(raw["evidence_stage_filter"]),
                status=DemoPoolStatus(str(raw["status"])),
                risk_budget=Decimal(str(raw["risk_budget"])),
                research_source=str(raw["research_source"]),
                first_seen_ms=int(raw["first_seen_ms"]),
                last_evaluated_ms=(
                    int(raw["last_evaluated_ms"])
                    if raw.get("last_evaluated_ms") is not None
                    else None
                ),
                rank=int(raw["rank"]) if raw.get("rank") is not None else None,
                decision=str(raw["decision"]) if raw.get("decision") is not None else None,
                reasons=tuple(str(value) for value in reasons_raw),
                metrics=dict(metrics_raw),
                evidence=dict(evidence_raw),
                live_trading_locked=bool(raw.get("live_trading_locked", False)),
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise PersistenceError("multi-strategy Demo pool entry is malformed") from exc


@dataclass(frozen=True, slots=True)
class StrategyPoolState:
    revision: int
    updated_at_ms: int
    entries: tuple[StrategyPoolEntry, ...]
    live_trading_locked: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": POOL_SCHEMA_VERSION,
            "revision": self.revision,
            "updated_at_ms": self.updated_at_ms,
            "entries": [entry.to_dict() for entry in self.entries],
            "live_trading_locked": self.live_trading_locked,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> StrategyPoolState:
        if raw.get("schema_version") != POOL_SCHEMA_VERSION:
            raise PersistenceError("multi-strategy Demo pool schema version is invalid")
        entries_raw = raw.get("entries")
        if not isinstance(entries_raw, list) or any(
            not isinstance(item, dict) for item in entries_raw
        ):
            raise PersistenceError("multi-strategy Demo pool entries are invalid")
        state = cls(
            revision=int(raw.get("revision", 0)),
            updated_at_ms=int(raw.get("updated_at_ms", 0)),
            entries=tuple(StrategyPoolEntry.from_dict(item) for item in entries_raw),
            live_trading_locked=bool(raw.get("live_trading_locked", False)),
        )
        state.validate()
        return state

    def validate(self) -> None:
        if self.revision < 1:
            raise PersistenceError("multi-strategy Demo pool revision is invalid")
        if not self.live_trading_locked:
            raise PersistenceError("multi-strategy Demo pool must keep LIVE trading locked")
        ids = tuple(entry.strategy_id for entry in self.entries)
        if len(ids) != len(set(ids)):
            raise PersistenceError("multi-strategy Demo pool has duplicate strategy ids")
        if set(ids) != {definition.strategy_id for definition in STRATEGY_POOL_DEFINITIONS}:
            raise PersistenceError(
                "multi-strategy Demo pool membership drifted from frozen registry"
            )
        for entry in self.entries:
            definition = strategy_definition(entry.strategy_id)
            if entry.evidence_stage_filter != definition.evidence_stage_filter:
                raise PersistenceError("strategy evidence stage filter drifted")
            if entry.risk_budget != definition.risk_budget:
                raise PersistenceError("strategy risk budget drifted")
            if not entry.live_trading_locked:
                raise PersistenceError("strategy entry must keep LIVE trading locked")


STRATEGY_POOL_DEFINITIONS = (
    StrategyPoolDefinition(
        REGIME_SPECIALIST_STRATEGY_ID,
        "Regime Specialist 28D25",
        REGIME_FORWARD_DEMO_STAGE,
        BULL_RISK_FRACTION,
        "USER_APPROVED_REGIME_RESEARCH_2012_2026",
    ),
    StrategyPoolDefinition(
        VOLATILITY_BREAKOUT_STRATEGY_ID,
        "Volatility Breakout 30/15 VT20",
        PARALLEL_FORWARD_DEMO_STAGE,
        VOL_BREAKOUT_RISK,
        "STANDARDIZED_FUTURES_RESEARCH_VOL30_15_VT20",
    ),
    StrategyPoolDefinition(
        FUNDING_OI_STRATEGY_ID,
        "Funding Z2 + OI Expansion >=2%",
        PARALLEL_FORWARD_DEMO_STAGE,
        FUNDING_OI_RISK,
        "BINANCE_FUNDING_AND_DAILY_OI_RESEARCH",
    ),
)


def strategy_definition(strategy_id: str) -> StrategyPoolDefinition:
    for definition in STRATEGY_POOL_DEFINITIONS:
        if definition.strategy_id == strategy_id:
            return definition
    raise PersistenceError(f"unknown Demo strategy id: {strategy_id}")


class _StrategyPoolRest(SupabaseRestClient):
    def select_state(self) -> StrategyPoolState | None:
        response = read_with_retry(
            self._client,
            f"{self.base_url}/rest/v1/runtime_state",
            params={
                "select": "version,state",
                "state_key": f"eq.{POOL_STATE_KEY}",
                "limit": "1",
            },
            operation="STRATEGY_POOL_STATE",
            headers=self._headers(),
        )
        if response.is_error:
            raise PersistenceError(
                f"strategy pool read failed status={response.status_code}: "
                f"{response.text[:300]}"
            )
        payload = response.json()
        if not isinstance(payload, list) or any(
            not isinstance(row, dict) for row in payload
        ):
            raise PersistenceError("strategy pool state response is invalid")
        if not payload:
            return None
        version = payload[0].get("version")
        raw = payload[0].get("state")
        if not isinstance(version, int) or not isinstance(raw, dict):
            raise PersistenceError("strategy pool state row is invalid")
        state = StrategyPoolState.from_dict(raw)
        if state.revision != version:
            raise PersistenceError("strategy pool runtime state revision mismatch")
        return state

    def save_state(
        self,
        state: StrategyPoolState,
        *,
        expected_revision: int | None,
    ) -> None:
        state.validate()
        row = {
            "state_key": POOL_STATE_KEY,
            "version": state.revision,
            "state": state.to_dict(),
            "updated_at_ms": state.updated_at_ms,
        }
        url = f"{self.base_url}/rest/v1/runtime_state"
        if expected_revision is None:
            response = self._client.post(
                url,
                params={"on_conflict": "state_key"},
                headers=self._headers(
                    prefer="resolution=ignore-duplicates,return=representation"
                ),
                json=(row,),
            )
        else:
            response = self._client.patch(
                url,
                params={
                    "state_key": f"eq.{POOL_STATE_KEY}",
                    "version": f"eq.{expected_revision}",
                },
                headers=self._headers(prefer="return=representation"),
                json=row,
            )
        if response.is_error:
            raise PersistenceError(
                f"strategy pool state write failed status={response.status_code}: "
                f"{response.text[:300]}"
            )
        payload = response.json()
        if not isinstance(payload, list) or len(payload) != 1:
            raise PersistenceError("strategy pool state lost optimistic concurrency race")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def seed_strategy_pool(*, now_ms: int | None = None) -> StrategyPoolState:
    timestamp = _now_ms() if now_ms is None else now_ms
    state = StrategyPoolState(
        revision=1,
        updated_at_ms=timestamp,
        entries=tuple(
            StrategyPoolEntry(
                strategy_id=definition.strategy_id,
                display_name=definition.display_name,
                evidence_stage_filter=definition.evidence_stage_filter,
                status=DemoPoolStatus.FORWARD_DEMO_ACTIVE,
                risk_budget=definition.risk_budget,
                research_source=definition.research_source,
                first_seen_ms=timestamp,
                last_evaluated_ms=None,
                rank=None,
                decision=None,
                reasons=("FORWARD_DEMO_EVIDENCE_PENDING",),
                metrics={},
                evidence={},
            )
            for definition in STRATEGY_POOL_DEFINITIONS
        ),
    )
    state.validate()
    return state


def read_strategy_pool(
    config: SupabasePersistenceConfig,
    *,
    client: httpx.Client | None = None,
) -> StrategyPoolState | None:
    config.validate()
    if not config.enabled:
        return None
    with _StrategyPoolRest(config, client=client) as rest:
        return rest.select_state()


def save_strategy_pool(
    config: SupabasePersistenceConfig,
    state: StrategyPoolState,
    *,
    expected_revision: int | None,
    client: httpx.Client | None = None,
) -> None:
    with _StrategyPoolRest(config, client=client) as rest:
        rest.save_state(state, expected_revision=expected_revision)


def ensure_strategy_pool(
    config: SupabasePersistenceConfig,
    *,
    now_ms: int | None = None,
    client: httpx.Client | None = None,
) -> StrategyPoolState:
    current = read_strategy_pool(config, client=client)
    if current is not None:
        return current
    seeded = seed_strategy_pool(now_ms=now_ms)
    save_strategy_pool(config, seeded, expected_revision=None, client=client)
    reread = read_strategy_pool(config, client=client)
    if reread is None:
        raise PersistenceError("strategy pool seed write was not observable")
    return reread


def strategy_demo_execution_authorized(
    config: SupabasePersistenceConfig,
    strategy_id: str,
    *,
    client: httpx.Client | None = None,
) -> bool:
    strategy_definition(strategy_id)
    state = read_strategy_pool(config, client=client)
    # Backwards-compatible bootstrap: frozen strategies remain authorized until the
    # first pool-evaluator cycle creates the durable registry.
    if state is None:
        return True
    for entry in state.entries:
        if entry.strategy_id == strategy_id:
            return entry.execution_authorized
    raise PersistenceError("known strategy is missing from multi-strategy Demo pool")
