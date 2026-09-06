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


@dataclass(frozen=True, slots=True)
class CalibrationMetrics:
    sample_size: int
    win_rate: Decimal | None
    profit_factor: Decimal | None
    median_mae_r: Decimal | None
    median_mfe_r: Decimal | None


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
    if sample_size < 10:
        return "OBSERVE_ONLY", Decimal(0), Decimal(0), 0
    if sample_size < 20:
        return "MICRO_ADJUST", Decimal("0.02"), Decimal("0.01"), 5
    if sample_size < 50:
        return "BOUNDED_ADJUST", Decimal("0.03"), Decimal("0.015"), 10
    if sample_size < 100:
        return "STRONGER_BOUNDED", Decimal("0.04"), Decimal("0.02"), 15
    return "SERIOUS_CALIBRATION", Decimal("0.05"), Decimal("0.02"), 20


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
    return CalibrationMetrics(
        sample_size=sample_size,
        win_rate=win_rate,
        profit_factor=profit_factor,
        median_mae_r=median(mae) if mae else None,
        median_mfe_r=median(mfe) if mfe else None,
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

    if metrics.sample_size < 10:
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

    # Entry calibration may only tighten the original chase allowance; it never loosens it.
    if poor:
        tightened = _clamp(
            current.max_chase_atr - chase_step,
            Decimal("0.60"),
            Decimal("0.80"),
        )
        if tightened != current.max_chase_atr:
            max_chase = tightened
            reasons.append("TIGHTEN_ENTRY_CHASE_ON_WEAK_OUTCOMES")

    # SL calibration stays in a narrow ATR band around the original structure-based stop.
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

    # TP2 stays structural by default. On weak outcomes with evidence that excursions commonly
    # peak between 2R and 2.6R, cap only excessively distant structural TP2 at a >=2R target.
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

    proposed = StrategyParameters(
        stop_buffer_atr=stop_buffer,
        max_chase_atr=max_chase,
        min_rr_tp1=current.min_rr_tp1,
        min_rr_tp2=current.min_rr_tp2,
        tp2_cap_rr=tp2_cap,
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


def _eligible_trade_rows(config: SupabasePersistenceConfig) -> tuple[dict[str, object], ...]:
    payload = _fetch_json_list(
        config,
        "closed_trades",
        {
            "select": "trade_key,symbol,direction,net_pnl,mfe_r,mae_r,exit_time_ms",
            "calibration_eligible": "eq.true",
            "history_complete": "eq.true",
            "order": "exit_time_ms.asc",
            "limit": "1000",
        },
    )
    rows: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise PersistenceError("eligible trade row is invalid")
        if item.get("net_pnl") is None:
            raise PersistenceError("eligible trade is missing net_pnl")
        rows.append(item)
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
    applied = int(state.get("last_applied_sample_size", 0))
    if reviewed < 0 or applied < 0 or applied > reviewed:
        raise PersistenceError("runtime calibration sample counters are invalid")
    return params, reviewed, applied


def run_calibration() -> dict[str, object]:
    config = SupabasePersistenceConfig.from_environment()
    if not config.enabled:
        raise PersistenceError("calibration requires dedicated Crypto Scanner Supabase")

    rows = _eligible_trade_rows(config)
    metrics = calculate_metrics(rows)
    current, previous_reviewed, previous_applied = _read_runtime_state(config)
    proposal = propose_parameters(
        metrics,
        current,
        previous_reviewed_sample_size=previous_reviewed,
    )
    generated_at_ms = _now_ms()

    next_applied_sample = metrics.sample_size if proposal.applied else previous_applied
    state = {
        "config_version": STRATEGY_CONFIG_VERSION,
        "params": proposal.after.to_dict(),
        "last_reviewed_sample_size": metrics.sample_size,
        "last_applied_sample_size": next_applied_sample,
        "last_calibration_at_ms": generated_at_ms,
        "tier": proposal.tier,
    }

    calibration_id = f"cal-global-{generated_at_ms}"
    metadata = {
        "tier": proposal.tier,
        "reasons": list(proposal.reasons),
        "before": proposal.before.to_dict(),
        "after": proposal.after.to_dict(),
        "minimum_new_samples": proposal.minimum_new_samples,
        "previous_reviewed_sample_size": previous_reviewed,
        "eligible_only": True,
        "history_complete_only": True,
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
                    "applied": proposal.applied,
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

    status = "PASS_CALIBRATION_APPLIED" if proposal.applied else "PASS_CALIBRATION_OBSERVE"
    return {
        "status": status,
        "sample_size": metrics.sample_size,
        "tier": proposal.tier,
        "applied": proposal.applied,
        "reasons": list(proposal.reasons),
        "before": proposal.before.to_dict(),
        "after": proposal.after.to_dict(),
        "live_trading_locked": True,
    }


def main() -> None:
    print(json.dumps(run_calibration(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
