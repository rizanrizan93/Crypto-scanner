from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

import httpx

from crypto_scanner.persistence import (
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseRestClient,
)
from crypto_scanner.strategy_params import (
    DEFAULT_STRATEGY_PARAMETERS,
    STRATEGY_CONFIG_VERSION,
    STRATEGY_STATE_KEY,
    StrategyParameters,
)
from crypto_scanner.strategy_promotion import (
    PromotionStage,
    queue_strategy_candidate,
    read_promotion_state,
)


@dataclass(frozen=True, slots=True)
class CalibrationMetrics:
    sample_size: int
    win_rate: Decimal | None
    profit_factor: Decimal | None
    median_mae_r: Decimal | None
    median_mfe_r: Decimal | None
    mfe_ge_1r_count: int = 0
    mfe_ge_1r_giveback_loss_rate: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CalibrationProposal:
    tier: str
    metrics: CalibrationMetrics
    before: StrategyParameters
    after: StrategyParameters
    applied: bool
    reasons: tuple[str, ...]
    minimum_new_samples: int
    previous_reviewed_sample_size: int


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(high, max(low, value))


def _round_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).quantize(Decimal("1")) * step


def _tier(sample_size: int) -> tuple[str, Decimal, Decimal, int]:
    # Small Demo samples are descriptive only. Parameter mutation starts at 50
    # complete, eligible trades and then requires materially new evidence.
    if sample_size < 50:
        return "OBSERVE_ONLY", Decimal(0), Decimal(0), 0
    if sample_size < 100:
        return "BOUNDED_ADJUST", Decimal("0.02"), Decimal("0.01"), 20
    if sample_size < 200:
        return "STRONGER_BOUNDED", Decimal("0.03"), Decimal("0.015"), 30
    return "SERIOUS_CALIBRATION", Decimal("0.04"), Decimal("0.02"), 50


def _profit_lock_adjustment_step(tier: str) -> Decimal:
    if tier == "BOUNDED_ADJUST":
        return Decimal("0.05")
    if tier in {"STRONGER_BOUNDED", "SERIOUS_CALIBRATION"}:
        return Decimal("0.10")
    return Decimal(0)


def calculate_metrics(rows: tuple[dict[str, object], ...]) -> CalibrationMetrics:
    sample_size = len(rows)
    if not rows:
        return CalibrationMetrics(0, None, None, None, None)

    pnls = tuple(Decimal(str(row["net_pnl"])) for row in rows)
    wins = sum(pnl > 0 for pnl in pnls)
    gross_profit = sum((pnl for pnl in pnls if pnl > 0), Decimal(0))
    gross_loss = abs(sum((pnl for pnl in pnls if pnl < 0), Decimal(0)))
    win_rate = Decimal(wins) / Decimal(sample_size)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

    mae = tuple(
        value
        for row in rows
        if (value := _decimal_or_none(row.get("mae_r"))) is not None
    )
    mfe = tuple(
        value
        for row in rows
        if (value := _decimal_or_none(row.get("mfe_r"))) is not None
    )
    lock_candidates = tuple(
        row
        for row in rows
        if (value := _decimal_or_none(row.get("mfe_r"))) is not None
        and value >= Decimal("1.00")
    )
    giveback_losses = sum(
        Decimal(str(row["net_pnl"])) <= 0 for row in lock_candidates
    )
    giveback_rate = (
        Decimal(giveback_losses) / Decimal(len(lock_candidates))
        if lock_candidates
        else None
    )
    return CalibrationMetrics(
        sample_size=sample_size,
        win_rate=win_rate,
        profit_factor=profit_factor,
        median_mae_r=median(mae) if mae else None,
        median_mfe_r=median(mfe) if mfe else None,
        mfe_ge_1r_count=len(lock_candidates),
        mfe_ge_1r_giveback_loss_rate=giveback_rate,
    )


def propose_parameters(
    metrics: CalibrationMetrics,
    current: StrategyParameters,
    *,
    previous_reviewed_sample_size: int = 0,
) -> CalibrationProposal:
    current.validate()
    tier, chase_step, stop_step, minimum_new = _tier(metrics.sample_size)
    reasons: list[str] = []

    if metrics.sample_size < 50:
        return CalibrationProposal(
            tier=tier,
            metrics=metrics,
            before=current,
            after=current,
            applied=False,
            reasons=("INSUFFICIENT_ELIGIBLE_TRADES",),
            minimum_new_samples=minimum_new,
            previous_reviewed_sample_size=previous_reviewed_sample_size,
        )

    new_samples = metrics.sample_size - previous_reviewed_sample_size
    if new_samples < minimum_new:
        return CalibrationProposal(
            tier=tier,
            metrics=metrics,
            before=current,
            after=current,
            applied=False,
            reasons=("WAITING_FOR_NEW_EVIDENCE",),
            minimum_new_samples=minimum_new,
            previous_reviewed_sample_size=previous_reviewed_sample_size,
        )

    win_rate = metrics.win_rate or Decimal(0)
    pf = metrics.profit_factor
    poor = win_rate < Decimal("0.45") or (pf is not None and pf < Decimal("1.00"))
    good = win_rate >= Decimal("0.52") and (pf is None or pf >= Decimal("1.15"))

    max_chase = current.max_chase_atr
    stop_buffer = current.stop_buffer_atr
    tp2_cap = current.tp2_cap_rr
    profit_lock_activation = current.profit_lock_activation_r
    profit_lock_gap = current.profit_lock_gap_r

    if poor:
        tightened = _clamp(
            current.max_chase_atr - chase_step,
            Decimal("0.60"),
            Decimal("0.80"),
        )
        if tightened != current.max_chase_atr:
            max_chase = tightened
            reasons.append("TIGHTEN_ENTRY_CHASE_ON_WEAK_OUTCOMES")

    if (
        metrics.median_mae_r is not None
        and metrics.median_mfe_r is not None
        and metrics.median_mae_r >= Decimal("0.90")
        and metrics.median_mfe_r >= Decimal("2.00")
    ):
        widened = _clamp(
            current.stop_buffer_atr + stop_step,
            Decimal("0.12"),
            Decimal("0.20"),
        )
        if widened != current.stop_buffer_atr:
            stop_buffer = widened
            reasons.append("WIDEN_SL_BUFFER_WHEN_MAE_PRECEDES_STRONG_MFE")
    elif metrics.median_mae_r is not None and metrics.median_mae_r <= Decimal("0.45") and good:
        tightened_stop = _clamp(
            current.stop_buffer_atr - stop_step / Decimal(2),
            Decimal("0.12"),
            Decimal("0.20"),
        )
        if tightened_stop != current.stop_buffer_atr:
            stop_buffer = tightened_stop
            reasons.append("TIGHTEN_SL_BUFFER_ON_LOW_MAE_POSITIVE_EDGE")

    if poor and metrics.median_mfe_r is not None:
        if Decimal("2.00") <= metrics.median_mfe_r < Decimal("2.60"):
            raw_cap = _clamp(
                metrics.median_mfe_r,
                Decimal("2.00"),
                Decimal("2.40"),
            )
            proposed_cap = _round_to_step(raw_cap, Decimal("0.05"))
            if tp2_cap != proposed_cap:
                tp2_cap = proposed_cap
                reasons.append("CAP_DISTANT_TP2_TO_OBSERVED_MFE")
    elif (
        good
        and metrics.median_mfe_r is not None
        and metrics.median_mfe_r >= Decimal("2.80")
        and tp2_cap is not None
    ):
        tp2_cap = None
        reasons.append("RESTORE_FULL_STRUCTURAL_TP2_ON_STRONG_MFE")

    lock_step = _profit_lock_adjustment_step(tier)
    giveback_rate = metrics.mfe_ge_1r_giveback_loss_rate
    if (
        poor
        and lock_step > 0
        and metrics.mfe_ge_1r_count >= 3
        and giveback_rate is not None
        and giveback_rate >= Decimal("0.50")
    ):
        tightened_gap = _clamp(
            current.profit_lock_gap_r - lock_step,
            Decimal("0.75"),
            Decimal("1.25"),
        )
        if tightened_gap != current.profit_lock_gap_r:
            profit_lock_gap = tightened_gap
            reasons.append("TIGHTEN_PROFIT_LOCK_ON_MFE_GIVEBACK_LOSSES")
        if metrics.mfe_ge_1r_count >= 5 and giveback_rate >= Decimal("0.70"):
            earlier_activation = _clamp(
                current.profit_lock_activation_r - Decimal("0.05"),
                Decimal("0.90"),
                Decimal("1.10"),
            )
            if earlier_activation != current.profit_lock_activation_r:
                profit_lock_activation = earlier_activation
                reasons.append("ACTIVATE_PROFIT_LOCK_EARLIER_ON_PERSISTENT_GIVEBACK")
    elif (
        good
        and lock_step > 0
        and metrics.mfe_ge_1r_count >= 5
        and giveback_rate is not None
        and giveback_rate <= Decimal("0.20")
        and metrics.median_mfe_r is not None
        and metrics.median_mfe_r >= Decimal("2.50")
    ):
        wider_gap = _clamp(
            current.profit_lock_gap_r + lock_step,
            Decimal("0.75"),
            Decimal("1.25"),
        )
        if wider_gap != current.profit_lock_gap_r:
            profit_lock_gap = wider_gap
            reasons.append("GIVE_WINNERS_MORE_ROOM_ON_STRONG_LOW_GIVEBACK_EDGE")

    proposed = StrategyParameters(
        stop_buffer_atr=stop_buffer,
        max_chase_atr=max_chase,
        min_rr_tp1=current.min_rr_tp1,
        min_rr_tp2=current.min_rr_tp2,
        tp2_cap_rr=tp2_cap,
        profit_lock_activation_r=profit_lock_activation,
        profit_lock_gap_r=profit_lock_gap,
    )
    proposed.validate()
    applied = proposed != current
    if not reasons:
        reasons.append("NO_BOUNDED_PARAMETER_CHANGE_JUSTIFIED")

    return CalibrationProposal(
        tier=tier,
        metrics=metrics,
        before=current,
        after=proposed,
        applied=applied,
        reasons=tuple(reasons),
        minimum_new_samples=minimum_new,
        previous_reviewed_sample_size=previous_reviewed_sample_size,
    )


def _headers(config: SupabasePersistenceConfig) -> dict[str, str]:
    assert config.service_role_key is not None
    return {
        "apikey": config.service_role_key,
        "Authorization": f"Bearer {config.service_role_key}",
    }


def _fetch_json_list(
    config: SupabasePersistenceConfig,
    path: str,
    params: dict[str, str],
) -> list[object]:
    assert config.url is not None
    with httpx.Client(timeout=15.0) as client:
        response = client.get(
            f"{config.url.rstrip('/')}/rest/v1/{path}",
            params=params,
            headers=_headers(config),
        )
    if response.is_error:
        raise PersistenceError(
            f"calibration read failed path={path} status={response.status_code}: "
            f"{response.text[:300]}"
        )
    payload = response.json()
    if not isinstance(payload, list):
        raise PersistenceError(f"calibration read returned non-list path={path}")
    return payload


def _postgrest_in(values: tuple[str, ...]) -> str:
    if any(not value.replace("-", "").isalnum() for value in values):
        raise PersistenceError("calibration signal identity is invalid")
    return "in.(" + ",".join(values) + ")"


def _eligible_trade_rows(
    config: SupabasePersistenceConfig,
    *,
    strategy_id: str,
) -> tuple[dict[str, object], ...]:
    signal_ids_list: list[str] = []
    for offset in range(0, 10_000, 1000):
        signal_payload = _fetch_json_list(
            config,
            "signals",
            {
                "select": "signal_id",
                "evidence->>strategy_id": f"eq.{strategy_id}",
                "order": "created_at_ms.asc",
                "limit": "1000",
                "offset": str(offset),
            },
        )
        for item in signal_payload:
            if not isinstance(item, dict) or not item.get("signal_id"):
                raise PersistenceError("calibration strategy signal identity is missing")
            signal_ids_list.append(str(item["signal_id"]))
        if len(signal_payload) < 1000:
            break
    else:
        raise PersistenceError("calibration evidence exceeds bounded 10000-row audit window")
    if len(signal_ids_list) != len(set(signal_ids_list)):
        raise PersistenceError("calibration evidence contains duplicate signal identities")
    signal_ids = tuple(signal_ids_list)
    if not signal_ids:
        return ()
    payload = tuple(
        item
        for index in range(0, len(signal_ids), 100)
        for item in _fetch_json_list(
            config,
            "closed_trades",
            {
                "select": "trade_key,symbol,direction,net_pnl,mfe_r,mae_r,exit_time_ms",
                "calibration_eligible": "eq.true",
                "history_complete": "eq.true",
                "signal_id": _postgrest_in(signal_ids[index : index + 100]),
                "order": "exit_time_ms.asc",
                "limit": "100",
            },
        )
    )
    rows: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise PersistenceError("eligible trade row is invalid")
        if item.get("net_pnl") is None:
            raise PersistenceError("eligible trade is missing net_pnl")
        rows.append(item)
    rows.sort(key=lambda row: int(row["exit_time_ms"]))
    return tuple(rows)


def _read_runtime_state(
    config: SupabasePersistenceConfig,
) -> tuple[StrategyParameters, int, int]:
    payload = _fetch_json_list(
        config,
        "runtime_state",
        {
            "select": "state",
            "state_key": f"eq.{STRATEGY_STATE_KEY}",
            "limit": "1",
        },
    )
    if not payload:
        return DEFAULT_STRATEGY_PARAMETERS, 0, 0
    row = payload[0]
    if not isinstance(row, dict) or not isinstance(row.get("state"), dict):
        raise PersistenceError("runtime calibration state is invalid")
    state = row["state"]
    assert isinstance(state, dict)
    params = StrategyParameters.from_mapping(state)
    reviewed = int(state.get("last_reviewed_sample_size", 0))
    proposed = int(
        state.get("last_candidate_sample_size", state.get("last_applied_sample_size", 0))
    )
    if reviewed < 0 or proposed < 0 or proposed > reviewed:
        raise PersistenceError("runtime calibration sample counters are invalid")
    return params, reviewed, proposed


def run_calibration() -> dict[str, object]:
    config = SupabasePersistenceConfig.from_environment()
    if not config.enabled:
        raise PersistenceError("calibration requires dedicated Crypto Scanner Supabase")

    promotion = read_promotion_state(config)
    if promotion is None or promotion.champion is None:
        return {
            "status": "PASS_CALIBRATION_DEFERRED_NO_CHAMPION",
            "applied": False,
            "live_trading_locked": True,
        }
    if promotion.stage is PromotionStage.QUARANTINED:
        return {
            "status": "PASS_CALIBRATION_DEFERRED_QUARANTINED",
            "stage": promotion.stage.value,
            "champion_strategy_id": promotion.champion.strategy_id,
            "applied": False,
            "live_trading_locked": True,
        }
    if promotion.stage in {PromotionStage.HISTORICAL_PENDING, PromotionStage.FORWARD_DEMO}:
        return {
            "status": "PASS_CALIBRATION_DEFERRED_ACTIVE_PROMOTION",
            "stage": promotion.stage.value,
            "champion_strategy_id": promotion.champion.strategy_id,
            "applied": False,
            "live_trading_locked": True,
        }
    rows = _eligible_trade_rows(config, strategy_id=promotion.champion.strategy_id)
    metrics = calculate_metrics(rows)
    current, previous_reviewed, previous_candidate = _read_runtime_state(config)
    proposal = propose_parameters(
        metrics,
        current,
        previous_reviewed_sample_size=previous_candidate,
    )
    generated_at_ms = _now_ms()

    calibration_id = f"cal-global-{generated_at_ms}"
    candidate_queued = False
    candidate_strategy_id: str | None = None
    if proposal.applied:
        promotion_state, candidate_queued = queue_strategy_candidate(
            config,
            proposal.after,
            source=f"CALIBRATION:{calibration_id}",
            now_ms=generated_at_ms,
        )
        if promotion_state.candidate is not None:
            candidate_strategy_id = promotion_state.candidate.strategy_id
    next_candidate_sample = metrics.sample_size if proposal.applied else previous_candidate
    state = {
        "config_version": STRATEGY_CONFIG_VERSION,
        "params": current.to_dict(),
        "last_reviewed_sample_size": metrics.sample_size,
        "last_candidate_sample_size": next_candidate_sample,
        "last_calibration_at_ms": generated_at_ms,
        "tier": proposal.tier,
        "candidate_strategy_id": candidate_strategy_id,
    }

    metadata = {
        "tier": proposal.tier,
        "reasons": list(proposal.reasons),
        "before": proposal.before.to_dict(),
        "after": proposal.after.to_dict(),
        "minimum_new_samples": proposal.minimum_new_samples,
        "previous_reviewed_sample_size": previous_reviewed,
        "previous_candidate_sample_size": previous_candidate,
        "evidence_baseline_sample_size": previous_candidate,
        "candidate_queued": candidate_queued,
        "candidate_strategy_id": candidate_strategy_id,
        "active_parameters_unchanged": True,
        "eligible_only": True,
        "history_complete_only": True,
        "champion_strategy_id": promotion.champion.strategy_id,
        "mfe_ge_1r_count": metrics.mfe_ge_1r_count,
        "mfe_ge_1r_giveback_loss_rate": metrics.mfe_ge_1r_giveback_loss_rate,
        "live_trading_locked": True,
        "risk_unchanged": True,
        "leverage_unchanged": True,
    }

    with SupabaseRestClient(config) as rest:
        rest.upsert(
            "calibration_stats",
            (
                {
                    "calibration_id": calibration_id,
                    "generated_at_ms": generated_at_ms,
                    "sample_size": metrics.sample_size,
                    "win_rate": metrics.win_rate,
                    "median_mae_r": metrics.median_mae_r,
                    "median_mfe_r": metrics.median_mfe_r,
                    "profit_factor": metrics.profit_factor,
                    "applied": False,
                    "config_version": STRATEGY_CONFIG_VERSION,
                    "metadata": metadata,
                },
            ),
            on_conflict=("calibration_id",),
        )
        rest.upsert(
            "runtime_state",
            (
                {
                    "state_key": STRATEGY_STATE_KEY,
                    "version": 1,
                    "state": state,
                    "updated_at_ms": generated_at_ms,
                },
            ),
            on_conflict=("state_key",),
        )

    status = (
        "PASS_CALIBRATION_CHALLENGER_QUEUED"
        if candidate_queued
        else "PASS_CALIBRATION_OBSERVE"
    )
    return {
        "status": status,
        "sample_size": metrics.sample_size,
        "tier": proposal.tier,
        "proposed": proposal.applied,
        "candidate_queued": candidate_queued,
        "candidate_strategy_id": candidate_strategy_id,
        "applied": False,
        "reasons": list(proposal.reasons),
        "before": proposal.before.to_dict(),
        "after": proposal.after.to_dict(),
        "mfe_ge_1r_count": metrics.mfe_ge_1r_count,
        "mfe_ge_1r_giveback_loss_rate": (
            str(metrics.mfe_ge_1r_giveback_loss_rate)
            if metrics.mfe_ge_1r_giveback_loss_rate is not None
            else None
        ),
        "live_trading_locked": True,
    }


def main() -> None:
    print(json.dumps(run_calibration(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
