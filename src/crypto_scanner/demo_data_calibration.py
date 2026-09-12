from __future__ import annotations

import json
import time

from crypto_scanner.calibration import (
    _eligible_trade_rows,
    _fetch_json_list,
    calculate_metrics,
    propose_parameters,
    run_calibration,
)
from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig, SupabaseRestClient
from crypto_scanner.strategy_params import STRATEGY_CONFIG_VERSION
from crypto_scanner.strategy_promotion import (
    PromotionStage,
    StrategyVersion,
    queue_strategy_candidate,
    read_promotion_state,
)

DEMO_CALIBRATION_REVIEW_STATE_KEY = "strategy_demo_data_calibration_review_v1"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def demo_calibration_subject(
    promotion_stage: PromotionStage,
    *,
    champion: StrategyVersion | None,
    candidate: StrategyVersion | None,
) -> StrategyVersion | None:
    """Return the research strategy whose actual Demo trades may tune a challenger."""

    if champion is not None:
        return None
    if promotion_stage is not PromotionStage.HISTORICAL_REJECTED:
        return None
    return candidate


def _read_review_state(
    config: SupabasePersistenceConfig,
    *,
    strategy_id: str,
) -> tuple[int, int]:
    payload = _fetch_json_list(
        config,
        "runtime_state",
        {
            "select": "state",
            "state_key": f"eq.{DEMO_CALIBRATION_REVIEW_STATE_KEY}",
            "limit": "1",
        },
    )
    if not payload:
        return 0, 0
    row = payload[0]
    if not isinstance(row, dict) or not isinstance(row.get("state"), dict):
        raise PersistenceError("Demo data calibration review state is invalid")
    state = row["state"]
    assert isinstance(state, dict)
    if str(state.get("subject_strategy_id") or "") != strategy_id:
        return 0, 0
    reviewed = int(state.get("last_reviewed_sample_size", 0))
    proposed = int(state.get("last_candidate_sample_size", 0))
    if reviewed < 0 or proposed < 0 or proposed > reviewed:
        raise PersistenceError("Demo data calibration sample counters are invalid")
    return reviewed, proposed


def run_demo_data_calibration() -> dict[str, object]:
    config = SupabasePersistenceConfig.from_environment()
    if not config.enabled:
        raise PersistenceError("Demo data calibration requires dedicated Crypto Scanner Supabase")

    promotion = read_promotion_state(config)
    if promotion is None:
        return {
            "status": "PASS_DEMO_DATA_CALIBRATION_DEFERRED_NO_PROMOTION_STATE",
            "applied": False,
            "live_trading_locked": True,
        }
    if promotion.champion is not None:
        # Once a champion exists, retain the established production-like calibration path.
        return run_calibration()
    if promotion.stage is PromotionStage.QUARANTINED:
        return {
            "status": "PASS_DEMO_DATA_CALIBRATION_DEFERRED_QUARANTINED",
            "stage": promotion.stage.value,
            "applied": False,
            "live_trading_locked": True,
        }

    subject = demo_calibration_subject(
        promotion.stage,
        champion=promotion.champion,
        candidate=promotion.candidate,
    )
    if subject is None:
        return {
            "status": "PASS_DEMO_DATA_CALIBRATION_DEFERRED_STAGE",
            "stage": promotion.stage.value,
            "applied": False,
            "live_trading_locked": True,
        }

    rows = _eligible_trade_rows(config, strategy_id=subject.strategy_id)
    metrics = calculate_metrics(rows)
    previous_reviewed, previous_candidate = _read_review_state(
        config,
        strategy_id=subject.strategy_id,
    )
    proposal = propose_parameters(
        metrics,
        subject.params,
        previous_reviewed_sample_size=previous_candidate,
    )
    generated_at_ms = _now_ms()
    calibration_id = f"cal-demo-data-{generated_at_ms}"

    candidate_queued = False
    challenger_strategy_id: str | None = None
    if proposal.applied:
        next_state, candidate_queued = queue_strategy_candidate(
            config,
            proposal.after,
            source=f"DEMO_DATA_CALIBRATION:{calibration_id}",
            now_ms=generated_at_ms,
        )
        if next_state.candidate is not None:
            challenger_strategy_id = next_state.candidate.strategy_id

    next_candidate_sample = metrics.sample_size if proposal.applied else previous_candidate
    review_state = {
        "config_version": STRATEGY_CONFIG_VERSION,
        "subject_role": "HISTORICAL_REJECTED_CANDIDATE",
        "subject_strategy_id": subject.strategy_id,
        "last_reviewed_sample_size": metrics.sample_size,
        "last_candidate_sample_size": next_candidate_sample,
        "last_calibration_at_ms": generated_at_ms,
        "tier": proposal.tier,
        "challenger_strategy_id": challenger_strategy_id,
        "actual_demo_trades_only": True,
        "forward_demo_credit": False,
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
        "challenger_strategy_id": challenger_strategy_id,
        "subject_role": "HISTORICAL_REJECTED_CANDIDATE",
        "subject_strategy_id": subject.strategy_id,
        "actual_demo_trades_only": True,
        "eligible_only": True,
        "history_complete_only": True,
        "promotion_credit": False,
        "forward_demo_credit": False,
        "active_parameters_unchanged": True,
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
                    "state_key": DEMO_CALIBRATION_REVIEW_STATE_KEY,
                    "version": 1,
                    "state": review_state,
                    "updated_at_ms": generated_at_ms,
                },
            ),
            on_conflict=("state_key",),
        )

    status = (
        "PASS_DEMO_DATA_CALIBRATION_CHALLENGER_QUEUED"
        if candidate_queued
        else "PASS_DEMO_DATA_CALIBRATION_OBSERVE"
    )
    return {
        "status": status,
        "stage": promotion.stage.value,
        "subject_strategy_id": subject.strategy_id,
        "sample_size": metrics.sample_size,
        "tier": proposal.tier,
        "proposed": proposal.applied,
        "candidate_queued": candidate_queued,
        "challenger_strategy_id": challenger_strategy_id,
        "applied": False,
        "reasons": list(proposal.reasons),
        "before": proposal.before.to_dict(),
        "after": proposal.after.to_dict(),
        "actual_demo_trades_only": True,
        "promotion_credit": False,
        "forward_demo_credit": False,
        "live_trading_locked": True,
    }


def main() -> None:
    print(json.dumps(run_demo_data_calibration(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
