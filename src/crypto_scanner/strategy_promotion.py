from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

import httpx

from crypto_scanner.persistence import (
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseRestClient,
)
from crypto_scanner.strategy_params import (
    StrategyParameters,
    load_strategy_parameters,
)

PROMOTION_STATE_KEY = "strategy_promotion_v1"
PROMOTION_SCHEMA_VERSION = "strategy-promotion-v1"

MIN_HISTORICAL_TRADES = 200
MIN_HISTORICAL_OOS_TRADES = 50
MIN_HISTORICAL_PROFIT_FACTOR = Decimal("1.20")
MIN_HISTORICAL_EXPECTANCY_R = Decimal("0.10")
MAX_HISTORICAL_DRAWDOWN_R = Decimal("10")
WALK_FORWARD_FOLDS = 4
MIN_POSITIVE_WALK_FORWARD_FOLDS = 3
MIN_ROBUSTNESS_PROFIT_FACTOR = Decimal("1.05")

MIN_FORWARD_DEMO_TRADES = 30
MIN_FORWARD_DEMO_DURATION_MS = 14 * 24 * 60 * 60 * 1000
MIN_FORWARD_PROFIT_FACTOR = Decimal("1.10")
MIN_FORWARD_EXPECTANCY_R = Decimal("0.05")
MAX_FORWARD_DRAWDOWN_R = Decimal("6")
EARLY_ROLLBACK_MIN_TRADES = 10
EARLY_ROLLBACK_PROFIT_FACTOR = Decimal("0.75")


class PromotionStage(StrEnum):
    HISTORICAL_PENDING = "HISTORICAL_PENDING"
    HISTORICAL_REJECTED = "HISTORICAL_REJECTED"
    FORWARD_DEMO = "FORWARD_DEMO"
    PROMOTED = "PROMOTED"
    ROLLED_BACK = "ROLLED_BACK"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class StrategyVersion:
    strategy_id: str
    params: StrategyParameters
    created_at_ms: int
    source: str

    def validate(self) -> None:
        if not self.strategy_id.startswith("strategy-"):
            raise PersistenceError("strategy version id is invalid")
        self.params.validate()
        if self.created_at_ms < 0 or not self.source.strip():
            raise PersistenceError("strategy version metadata is invalid")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "strategy_id": self.strategy_id,
            "params": self.params.to_dict(),
            "created_at_ms": self.created_at_ms,
            "source": self.source,
        }

    @classmethod
    def from_mapping(cls, value: object) -> StrategyVersion:
        if not isinstance(value, dict):
            raise PersistenceError("strategy version state must be an object")
        result = cls(
            strategy_id=str(value.get("strategy_id") or ""),
            params=StrategyParameters.from_mapping(value),
            created_at_ms=int(value.get("created_at_ms") or 0),
            source=str(value.get("source") or ""),
        )
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    sample_size: int
    wins: int
    losses: int
    win_rate: Decimal | None
    expectancy_r: Decimal | None
    profit_factor: Decimal | None
    max_drawdown_r: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "sample_size": self.sample_size,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": str(self.win_rate) if self.win_rate is not None else None,
            "expectancy_r": (
                str(self.expectancy_r) if self.expectancy_r is not None else None
            ),
            "profit_factor": (
                str(self.profit_factor) if self.profit_factor is not None else None
            ),
            "max_drawdown_r": str(self.max_drawdown_r),
        }


@dataclass(frozen=True, slots=True)
class HistoricalGateResult:
    passed: bool
    reasons: tuple[str, ...]
    overall: PerformanceMetrics
    oos: PerformanceMetrics
    positive_walk_forward_folds: int
    walk_forward_folds: int
    robustness_min_profit_factor: Decimal | None

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "overall": self.overall.to_dict(),
            "oos": self.oos.to_dict(),
            "positive_walk_forward_folds": self.positive_walk_forward_folds,
            "walk_forward_folds": self.walk_forward_folds,
            "robustness_min_profit_factor": (
                str(self.robustness_min_profit_factor)
                if self.robustness_min_profit_factor is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class ForwardDemoGateResult:
    decision: str
    reasons: tuple[str, ...]
    metrics: PerformanceMetrics
    safety_incident_count: int
    evidence_duration_ms: int | None

    @property
    def passed(self) -> bool:
        return self.decision == "PROMOTE"

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "metrics": self.metrics.to_dict(),
            "safety_incident_count": self.safety_incident_count,
            "evidence_duration_ms": self.evidence_duration_ms,
        }


@dataclass(frozen=True, slots=True)
class StrategyPromotionState:
    stage: PromotionStage
    revision: int
    champion: StrategyVersion | None
    candidate: StrategyVersion | None
    historical_evidence: dict[str, object] | None
    forward_evidence: dict[str, object] | None
    updated_at_ms: int
    reason: str

    def validate(self) -> None:
        if self.revision < 1 or self.updated_at_ms < 0 or not self.reason.strip():
            raise PersistenceError("strategy promotion metadata is invalid")
        if self.champion is not None:
            self.champion.validate()
        if self.candidate is not None:
            self.candidate.validate()
        if self.stage in {
            PromotionStage.HISTORICAL_PENDING,
            PromotionStage.HISTORICAL_REJECTED,
            PromotionStage.FORWARD_DEMO,
        } and self.candidate is None:
            raise PersistenceError("promotion stage requires a candidate strategy")
        if self.stage is PromotionStage.PROMOTED and self.champion is None:
            raise PersistenceError("PROMOTED stage requires a champion strategy")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": PROMOTION_SCHEMA_VERSION,
            "stage": self.stage.value,
            "revision": self.revision,
            "champion": self.champion.to_dict() if self.champion else None,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "historical_evidence": self.historical_evidence,
            "forward_evidence": self.forward_evidence,
            "updated_at_ms": self.updated_at_ms,
            "reason": self.reason,
            "live_trading_locked": True,
            "risk_calibration_forbidden": True,
            "leverage_calibration_forbidden": True,
        }

    @classmethod
    def from_mapping(cls, value: object) -> StrategyPromotionState:
        if not isinstance(value, dict):
            raise PersistenceError("strategy promotion state must be an object")
        if value.get("schema_version") != PROMOTION_SCHEMA_VERSION:
            raise PersistenceError("strategy promotion schema version is invalid")
        try:
            stage = PromotionStage(str(value.get("stage") or ""))
        except ValueError as exc:
            raise PersistenceError("strategy promotion stage is invalid") from exc
        champion = value.get("champion")
        candidate = value.get("candidate")
        result = cls(
            stage=stage,
            revision=int(value.get("revision") or 0),
            champion=StrategyVersion.from_mapping(champion) if champion else None,
            candidate=StrategyVersion.from_mapping(candidate) if candidate else None,
            historical_evidence=(
                dict(value["historical_evidence"])
                if isinstance(value.get("historical_evidence"), dict)
                else None
            ),
            forward_evidence=(
                dict(value["forward_evidence"])
                if isinstance(value.get("forward_evidence"), dict)
                else None
            ),
            updated_at_ms=int(value.get("updated_at_ms") or 0),
            reason=str(value.get("reason") or ""),
        )
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class StrategyRuntimeSelection:
    strategy_id: str
    params: StrategyParameters
    promotion_stage: str
    execution_authorized: bool
    forward_demo_candidate: bool


class _PromotionRestClient(SupabaseRestClient):
    def select_runtime_state(self, state_key: str) -> dict[str, object] | None:
        response = self._client.get(
            f"{self.base_url}/rest/v1/runtime_state",
            params={
                "select": "version,state",
                "state_key": f"eq.{state_key}",
                "limit": "1",
            },
            headers=self._headers(),
        )
        if response.is_error:
            raise PersistenceError(
                f"promotion state read failed status={response.status_code}: "
                f"{response.text[:300]}"
            )
        payload = response.json()
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise PersistenceError("promotion state response is invalid")
        if not payload:
            return None
        row_version = payload[0].get("version")
        state = payload[0].get("state")
        if not isinstance(state, dict):
            raise PersistenceError("promotion runtime state payload is invalid")
        if not isinstance(row_version, int):
            raise PersistenceError("promotion runtime state version is invalid")
        state_revision = state.get("revision")
        if state_revision is not None and int(state_revision) != row_version:
            raise PersistenceError("promotion runtime state revision mismatch")
        return state

    def transition_runtime_state(
        self,
        *,
        state_key: str,
        version: int,
        state: dict[str, object],
        updated_at_ms: int,
        expected_version: int | None,
    ) -> None:
        """Persist one state transition with optimistic concurrency control."""

        row = {
            "state_key": state_key,
            "version": version,
            "state": state,
            "updated_at_ms": updated_at_ms,
        }
        url = f"{self.base_url}/rest/v1/runtime_state"
        if expected_version is None:
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
                    "state_key": f"eq.{state_key}",
                    "version": f"eq.{expected_version}",
                },
                headers=self._headers(prefer="return=representation"),
                json=row,
            )
        if response.is_error:
            raise PersistenceError(
                "promotion state transition failed "
                f"status={response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        if not isinstance(payload, list) or len(payload) != 1:
            raise PersistenceError(
                "promotion state transition lost optimistic concurrency race"
            )


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def strategy_version_id(params: StrategyParameters) -> str:
    payload = json.dumps(params.to_dict(), sort_keys=True, separators=(",", ":"))
    return f"strategy-{hashlib.sha256(payload.encode()).hexdigest()[:20]}"


def make_strategy_version(
    params: StrategyParameters,
    *,
    source: str,
    created_at_ms: int | None = None,
) -> StrategyVersion:
    params.validate()
    return StrategyVersion(
        strategy_id=strategy_version_id(params),
        params=params,
        created_at_ms=_now_ms() if created_at_ms is None else created_at_ms,
        source=source,
    )


def calculate_performance(result_r: tuple[Decimal, ...]) -> PerformanceMetrics:
    if not result_r:
        return PerformanceMetrics(0, 0, 0, None, None, None, Decimal(0))
    wins = sum(value > 0 for value in result_r)
    losses = sum(value < 0 for value in result_r)
    gross_profit = sum((value for value in result_r if value > 0), Decimal(0))
    gross_loss = abs(sum((value for value in result_r if value < 0), Decimal(0)))
    equity = peak = max_drawdown = Decimal(0)
    for value in result_r:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    sample = Decimal(len(result_r))
    return PerformanceMetrics(
        len(result_r),
        wins,
        losses,
        Decimal(wins) / sample,
        sum(result_r, Decimal(0)) / sample,
        gross_profit / gross_loss if gross_loss > 0 else None,
        max_drawdown,
    )


def _pf_at_least(metrics: PerformanceMetrics, minimum: Decimal) -> bool:
    if metrics.profit_factor is not None:
        return metrics.profit_factor >= minimum
    return metrics.wins > 0 and metrics.losses == 0


def _walk_forward_oos_folds(
    values: tuple[Decimal, ...],
) -> tuple[tuple[Decimal, ...], ...]:
    """Return chronological OOS windows after an expanding training prefix.

    The first half is the initial training/history window. The remaining half
    is split into four forward-only test windows. No result from a later fold
    can leak into an earlier decision.
    """

    initial_train = len(values) // 2
    remaining = len(values) - initial_train
    return tuple(
        values[
            initial_train + (remaining * index) // WALK_FORWARD_FOLDS :
            initial_train + (remaining * (index + 1)) // WALK_FORWARD_FOLDS
        ]
        for index in range(WALK_FORWARD_FOLDS)
    )


def evaluate_historical_gate(
    result_r: tuple[Decimal, ...],
    *,
    robustness_result_sets: tuple[tuple[Decimal, ...], ...] = (),
) -> HistoricalGateResult:
    overall = calculate_performance(result_r)
    folds = _walk_forward_oos_folds(result_r)
    oos_values = tuple(value for fold in folds for value in fold)
    oos = calculate_performance(oos_values)
    positive_folds = sum(
        metrics.expectancy_r is not None
        and metrics.expectancy_r > 0
        and (metrics.profit_factor is None or metrics.profit_factor > 1)
        for metrics in (calculate_performance(fold) for fold in folds)
    )
    robustness_metrics = tuple(
        calculate_performance(
            tuple(
                value
                for fold in _walk_forward_oos_folds(values)
                for value in fold
            )
        )
        for values in robustness_result_sets
    )
    robustness_pf = tuple(
        metrics.profit_factor or Decimal("Infinity")
        for metrics in robustness_metrics
        if metrics.profit_factor is not None or (metrics.wins > 0 and metrics.losses == 0)
    )
    minimum_robustness = min(robustness_pf) if robustness_pf else None
    reasons: list[str] = []
    if overall.sample_size < MIN_HISTORICAL_TRADES:
        reasons.append("HISTORICAL_SAMPLE_BELOW_200")
    if oos.sample_size < MIN_HISTORICAL_OOS_TRADES:
        reasons.append("HISTORICAL_OOS_SAMPLE_BELOW_50")
    if not _pf_at_least(oos, MIN_HISTORICAL_PROFIT_FACTOR):
        reasons.append("HISTORICAL_OOS_PROFIT_FACTOR_BELOW_1_20")
    if oos.expectancy_r is None or oos.expectancy_r < MIN_HISTORICAL_EXPECTANCY_R:
        reasons.append("HISTORICAL_OOS_EXPECTANCY_BELOW_0_10R")
    if oos.max_drawdown_r > MAX_HISTORICAL_DRAWDOWN_R:
        reasons.append("HISTORICAL_OOS_DRAWDOWN_ABOVE_10R")
    if positive_folds < MIN_POSITIVE_WALK_FORWARD_FOLDS:
        reasons.append("HISTORICAL_WALK_FORWARD_FOLDS_UNSTABLE")
    if len(robustness_result_sets) < 2:
        reasons.append("HISTORICAL_PARAMETER_ROBUSTNESS_MISSING")
    elif minimum_robustness is None or minimum_robustness < MIN_ROBUSTNESS_PROFIT_FACTOR:
        reasons.append("HISTORICAL_PARAMETER_ROBUSTNESS_FAILED")
    return HistoricalGateResult(
        not reasons,
        tuple(reasons) if reasons else ("HISTORICAL_WALK_FORWARD_PASS",),
        overall,
        oos,
        positive_folds,
        WALK_FORWARD_FOLDS,
        minimum_robustness,
    )


def evaluate_forward_demo_gate(
    result_r: tuple[Decimal, ...],
    *,
    safety_incident_count: int,
    evidence_duration_ms: int | None = None,
) -> ForwardDemoGateResult:
    metrics = calculate_performance(result_r)
    if safety_incident_count > 0:
        return ForwardDemoGateResult(
            "QUARANTINE",
            ("FORWARD_DEMO_SAFETY_INCIDENT",),
            metrics,
            safety_incident_count,
            evidence_duration_ms,
        )
    if metrics.max_drawdown_r > MAX_FORWARD_DRAWDOWN_R:
        return ForwardDemoGateResult(
            "ROLLBACK",
            ("FORWARD_DEMO_DRAWDOWN_ABOVE_6R",),
            metrics,
            0,
            evidence_duration_ms,
        )
    if (
        metrics.sample_size >= EARLY_ROLLBACK_MIN_TRADES
        and metrics.profit_factor is not None
        and metrics.profit_factor < EARLY_ROLLBACK_PROFIT_FACTOR
    ):
        return ForwardDemoGateResult(
            "ROLLBACK",
            ("FORWARD_DEMO_EARLY_PROFIT_FACTOR_BELOW_0_75",),
            metrics,
            0,
            evidence_duration_ms,
        )
    if metrics.sample_size < MIN_FORWARD_DEMO_TRADES:
        return ForwardDemoGateResult(
            "WAIT",
            ("FORWARD_DEMO_SAMPLE_BELOW_30",),
            metrics,
            0,
            evidence_duration_ms,
        )
    reasons: list[str] = []
    if not _pf_at_least(metrics, MIN_FORWARD_PROFIT_FACTOR):
        reasons.append("FORWARD_DEMO_PROFIT_FACTOR_BELOW_1_10")
    if metrics.expectancy_r is None or metrics.expectancy_r < MIN_FORWARD_EXPECTANCY_R:
        reasons.append("FORWARD_DEMO_EXPECTANCY_BELOW_0_05R")
    if reasons:
        return ForwardDemoGateResult(
            "ROLLBACK", tuple(reasons), metrics, 0, evidence_duration_ms
        )
    if evidence_duration_ms is None or evidence_duration_ms < MIN_FORWARD_DEMO_DURATION_MS:
        return ForwardDemoGateResult(
            "WAIT",
            ("FORWARD_DEMO_DURATION_BELOW_14_DAYS",),
            metrics,
            0,
            evidence_duration_ms,
        )
    return ForwardDemoGateResult(
        "PROMOTE", ("FORWARD_DEMO_PASS",), metrics, 0, evidence_duration_ms
    )


def read_promotion_state(
    config: SupabasePersistenceConfig,
    *,
    client: httpx.Client | None = None,
) -> StrategyPromotionState | None:
    config.validate()
    if not config.enabled:
        return None
    with _PromotionRestClient(config, client=client) as rest:
        raw = rest.select_runtime_state(PROMOTION_STATE_KEY)
    return StrategyPromotionState.from_mapping(raw) if raw is not None else None


def save_promotion_state(
    config: SupabasePersistenceConfig,
    state: StrategyPromotionState,
    *,
    expected_revision: int | None = None,
    client: httpx.Client | None = None,
) -> None:
    state.validate()
    if expected_revision is None and state.revision != 1:
        raise PersistenceError(
            "non-initial promotion transition requires expected_revision"
        )
    if expected_revision is not None and state.revision != expected_revision + 1:
        raise PersistenceError("promotion transition revision is not monotonic")
    with _PromotionRestClient(config, client=client) as rest:
        rest.transition_runtime_state(
            state_key=PROMOTION_STATE_KEY,
            version=state.revision,
            state=state.to_dict(),
            updated_at_ms=state.updated_at_ms,
            expected_version=expected_revision,
        )


def queue_strategy_candidate(
    config: SupabasePersistenceConfig,
    params: StrategyParameters,
    *,
    source: str,
    now_ms: int | None = None,
    client: httpx.Client | None = None,
) -> tuple[StrategyPromotionState, bool]:
    timestamp = _now_ms() if now_ms is None else now_ms
    candidate = make_strategy_version(params, source=source, created_at_ms=timestamp)
    current = read_promotion_state(config, client=client)
    if current is not None:
        if current.candidate and current.candidate.strategy_id == candidate.strategy_id:
            return current, False
        if current.champion and current.champion.strategy_id == candidate.strategy_id:
            return current, False
        if current.stage in {
            PromotionStage.HISTORICAL_PENDING,
            PromotionStage.FORWARD_DEMO,
            PromotionStage.QUARANTINED,
        }:
            return current, False
    state = StrategyPromotionState(
        PromotionStage.HISTORICAL_PENDING,
        1 if current is None else current.revision + 1,
        None if current is None else current.champion,
        candidate,
        None,
        None,
        timestamp,
        "STRATEGY_CANDIDATE_QUEUED",
    )
    save_promotion_state(
        config,
        state,
        expected_revision=None if current is None else current.revision,
        client=client,
    )
    return state, True


def load_strategy_runtime(
    config: SupabasePersistenceConfig,
    *,
    client: httpx.Client | None = None,
) -> StrategyRuntimeSelection:
    active = load_strategy_parameters(config, client=client)
    state = read_promotion_state(config, client=client)
    if state is None:
        return StrategyRuntimeSelection(
            strategy_version_id(active), active, "UNVALIDATED", False, False
        )
    if state.stage is PromotionStage.FORWARD_DEMO:
        assert state.candidate is not None
        return StrategyRuntimeSelection(
            state.candidate.strategy_id, state.candidate.params, state.stage.value, True, True
        )
    if state.champion is not None:
        return StrategyRuntimeSelection(
            state.champion.strategy_id,
            state.champion.params,
            state.stage.value,
            state.stage is not PromotionStage.QUARANTINED,
            False,
        )
    params = state.candidate.params if state.candidate else active
    strategy_id = state.candidate.strategy_id if state.candidate else strategy_version_id(active)
    return StrategyRuntimeSelection(strategy_id, params, state.stage.value, False, False)


def record_historical_decision(
    state: StrategyPromotionState,
    result: HistoricalGateResult,
    *,
    now_ms: int | None = None,
) -> StrategyPromotionState:
    if state.stage is not PromotionStage.HISTORICAL_PENDING or state.candidate is None:
        raise PersistenceError("historical decision requires a pending candidate")
    return replace(
        state,
        stage=(
            PromotionStage.FORWARD_DEMO
            if result.passed
            else PromotionStage.HISTORICAL_REJECTED
        ),
        revision=state.revision + 1,
        historical_evidence=result.to_dict(),
        forward_evidence=None,
        updated_at_ms=_now_ms() if now_ms is None else now_ms,
        reason="HISTORICAL_GATE_PASSED" if result.passed else "HISTORICAL_GATE_REJECTED",
    )


def record_forward_decision(
    state: StrategyPromotionState,
    result: ForwardDemoGateResult,
    *,
    now_ms: int | None = None,
) -> StrategyPromotionState:
    if state.stage is not PromotionStage.FORWARD_DEMO or state.candidate is None:
        raise PersistenceError("forward decision requires a FORWARD_DEMO candidate")
    timestamp = _now_ms() if now_ms is None else now_ms
    if result.decision == "WAIT":
        return replace(
            state,
            revision=state.revision + 1,
            forward_evidence=result.to_dict(),
            updated_at_ms=timestamp,
            reason="FORWARD_DEMO_EVIDENCE_PENDING",
        )
    if result.decision == "PROMOTE":
        return StrategyPromotionState(
            PromotionStage.PROMOTED,
            state.revision + 1,
            state.candidate,
            None,
            state.historical_evidence,
            result.to_dict(),
            timestamp,
            "FORWARD_DEMO_GATE_PASSED_AUTO_PROMOTED",
        )
    stage = (
        PromotionStage.QUARANTINED
        if result.decision == "QUARANTINE" or state.champion is None
        else PromotionStage.ROLLED_BACK
    )
    return replace(
        state,
        stage=stage,
        revision=state.revision + 1,
        candidate=None,
        forward_evidence=result.to_dict(),
        updated_at_ms=timestamp,
        reason=(
            "FORWARD_DEMO_SAFETY_QUARANTINE"
            if stage is PromotionStage.QUARANTINED
            else "FORWARD_DEMO_FAILED_AUTO_ROLLBACK"
        ),
    )


def save_promoted_champion(
    config: SupabasePersistenceConfig,
    state: StrategyPromotionState,
    *,
    client: httpx.Client | None = None,
) -> None:
    if state.stage is not PromotionStage.PROMOTED or state.champion is None:
        raise PersistenceError("only a promoted champion can become active strategy state")
    # Promotion state is the single authoritative row used by every execution
    # path. A single compare-and-swap avoids a torn champion/promotion pair.
    save_promotion_state(
        config,
        state,
        expected_revision=state.revision - 1,
        client=client,
    )
