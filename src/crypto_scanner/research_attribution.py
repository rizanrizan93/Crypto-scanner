from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

import httpx

from crypto_scanner.hot_watch import RESEARCH_SCHEMA, RESEARCH_STRATEGY_ID
from crypto_scanner.persistence import (
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseRestClient,
)

RESEARCH_STATE_KEY = "research:factor_attribution:v1"
MIN_ATTRIBUTION_SAMPLES = 50
MIN_GROUP_SAMPLES = 15
MIN_INTERACTION_SAMPLES = 100
MIN_INTERACTION_GROUP_SAMPLES = 25


@dataclass(frozen=True, slots=True)
class ResearchTradeSample:
    signal_id: str
    symbol: str
    direction: str
    net_pnl: Decimal
    price_return_bps: Decimal
    net_return_bps: Decimal
    mfe_r: Decimal | None
    mae_r: Decimal | None
    telemetry: dict[str, object]


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _headers(config: SupabasePersistenceConfig) -> dict[str, str]:
    assert config.service_role_key is not None
    return {
        "apikey": config.service_role_key,
        "Authorization": f"Bearer {config.service_role_key}",
        "Content-Type": "application/json",
    }


def _fetch_rows(
    config: SupabasePersistenceConfig,
    table: str,
    params: dict[str, str],
) -> list[dict[str, object]]:
    if not table.replace("_", "").isalnum():
        raise PersistenceError("invalid research table name")
    assert config.url is not None
    with httpx.Client(timeout=15.0) as client:
        response = client.get(
            f"{config.url.rstrip('/')}/rest/v1/{table}",
            params=params,
            headers=_headers(config),
        )
    if response.is_error:
        raise PersistenceError(
            f"research read failed table={table} status={response.status_code}: "
            f"{response.text[:300]}"
        )
    payload = response.json()
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise PersistenceError(f"research read returned invalid rows table={table}")
    return payload


def _research_telemetry_by_signal(
    runtime_rows: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    selected: dict[str, tuple[int, dict[str, object]]] = {}
    for row in runtime_rows:
        state = row.get("state")
        if not isinstance(state, dict):
            continue
        signal_id = state.get("signal_id")
        telemetry = state.get("telemetry")
        if not isinstance(signal_id, str) or not isinstance(telemetry, dict):
            continue
        if telemetry.get("research_schema") != RESEARCH_SCHEMA:
            continue
        if telemetry.get("strategy_id") != RESEARCH_STRATEGY_ID:
            continue
        observed = int(state.get("observed_at_ms") or row.get("updated_at_ms") or 0)
        previous = selected.get(signal_id)
        if previous is None or observed >= previous[0]:
            selected[signal_id] = (observed, telemetry)
    return {signal_id: item[1] for signal_id, item in selected.items()}


def build_research_samples(
    closed_rows: list[dict[str, object]],
    runtime_rows: list[dict[str, object]],
) -> tuple[ResearchTradeSample, ...]:
    telemetry_by_signal = _research_telemetry_by_signal(runtime_rows)
    samples: list[ResearchTradeSample] = []
    for row in closed_rows:
        if not row.get("calibration_eligible") or not row.get("history_complete"):
            continue
        signal_id = row.get("signal_id")
        if not isinstance(signal_id, str):
            continue
        telemetry = telemetry_by_signal.get(signal_id)
        if telemetry is None:
            continue

        entry_price = _decimal(row.get("average_entry_price"))
        exit_price = _decimal(row.get("average_exit_price"))
        entry_qty = _decimal(row.get("entry_qty"))
        net_pnl = _decimal(row.get("net_pnl"))
        direction = str(row.get("direction") or "")
        if (
            entry_price is None
            or exit_price is None
            or entry_qty is None
            or net_pnl is None
            or entry_price <= 0
            or exit_price <= 0
            or entry_qty <= 0
            or direction not in {"LONG", "SHORT"}
        ):
            continue

        direction_sign = Decimal(1) if direction == "LONG" else Decimal(-1)
        price_return_bps = (
            (exit_price - entry_price) / entry_price * Decimal("10000") * direction_sign
        )
        entry_notional = entry_price * entry_qty
        net_return_bps = net_pnl / entry_notional * Decimal("10000")
        samples.append(
            ResearchTradeSample(
                signal_id=signal_id,
                symbol=str(row.get("symbol") or ""),
                direction=direction,
                net_pnl=net_pnl,
                price_return_bps=price_return_bps,
                net_return_bps=net_return_bps,
                mfe_r=_decimal(row.get("mfe_r")),
                mae_r=_decimal(row.get("mae_r")),
                telemetry=telemetry,
            )
        )
    return tuple(samples)


def _frame(sample: ResearchTradeSample, timeframe: str) -> dict[str, object] | None:
    frames = sample.telemetry.get("frames")
    if not isinstance(frames, dict):
        return None
    value = frames.get(timeframe)
    return value if isinstance(value, dict) else None


def _alignment(sample: ResearchTradeSample, timeframe: str) -> bool | None:
    frame = _frame(sample, timeframe)
    if frame is None:
        return None
    count = frame.get("alignment_count")
    return int(count) == 3 if count is not None else None


def _frame_metric_ge(
    sample: ResearchTradeSample,
    timeframe: str,
    field: str,
    threshold: Decimal,
) -> bool | None:
    frame = _frame(sample, timeframe)
    value = _decimal(frame.get(field)) if frame else None
    return value >= threshold if value is not None else None


def _frame_regime_trend(sample: ResearchTradeSample, timeframe: str) -> bool | None:
    frame = _frame(sample, timeframe)
    if frame is None:
        return None
    regime = frame.get("regime")
    if not isinstance(regime, str):
        return None
    return regime == "TREND"


def _known_all(*values: bool | None) -> bool | None:
    if any(value is None for value in values):
        return None
    return all(value is True for value in values)


def _factor_value(sample: ResearchTradeSample, factor: str) -> bool | None:
    telemetry = sample.telemetry
    if factor in {
        "trend_5m_full_alignment",
        "trend_15m_full_alignment",
        "trend_60m_full_alignment",
    }:
        timeframe = factor.split("_")[1].removesuffix("m")
        return _alignment(sample, timeframe)
    if factor in {"adx_5m_ge_20", "adx_15m_ge_20", "adx_60m_ge_20"}:
        timeframe = factor.split("_")[1].removesuffix("m")
        return _frame_metric_ge(sample, timeframe, "adx14", Decimal(20))
    if factor in {
        "momentum_5m_ge_1atr",
        "momentum_15m_ge_1atr",
        "momentum_60m_ge_1atr",
    }:
        timeframe = factor.split("_")[1].removesuffix("m")
        return _frame_metric_ge(
            sample,
            timeframe,
            "direction_signed_momentum_to_atr",
            Decimal(1),
        )
    if factor in {"regime_5m_trend", "regime_15m_trend", "regime_60m_trend"}:
        timeframe = factor.split("_")[1].removesuffix("m")
        return _frame_regime_trend(sample, timeframe)
    if factor == "atr_15m_expanding":
        return _frame_metric_ge(sample, "15", "atr_expansion", Decimal(1))
    if factor == "taker_flow_aligned":
        value = _decimal(telemetry.get("direction_signed_taker_pressure"))
        return value > 0 if value is not None else None
    if factor == "orderbook_flow_aligned":
        value = _decimal(telemetry.get("direction_signed_orderbook_imbalance"))
        return value > 0 if value is not None else None
    if factor == "move_1m_aligned":
        value = _decimal(telemetry.get("direction_signed_move_1m_bps"))
        return value > 0 if value is not None else None
    if factor == "move_3m_aligned":
        value = _decimal(telemetry.get("direction_signed_move_3m_bps"))
        return value > 0 if value is not None else None
    if factor == "one_minute_displacement":
        value = telemetry.get("one_minute_displacement")
        return value if isinstance(value, bool) else None
    if factor == "reclaim_or_retest_1m":
        value = telemetry.get("reclaim_or_retest_1m")
        return value if isinstance(value, bool) else None
    if factor == "spread_le_5bps":
        value = _decimal(telemetry.get("spread_bps"))
        return value <= Decimal(5) if value is not None else None
    if factor == "distance_le_5bps":
        value = _decimal(telemetry.get("distance_to_discovery_reference_bps"))
        return value <= Decimal(5) if value is not None else None
    if factor == "score_ge_60":
        value = _decimal(telemetry.get("discovery_score"))
        return value >= Decimal(60) if value is not None else None
    if factor == "score_separation_ge_8":
        value = _decimal(telemetry.get("score_separation"))
        return value >= Decimal(8) if value is not None else None
    if factor == "context_aligned":
        context = telemetry.get("context_bias")
        if context not in {"BULLISH", "BEARISH"}:
            return None
        return (context == "BULLISH" and sample.direction == "LONG") or (
            context == "BEARISH" and sample.direction == "SHORT"
        )
    if factor == "same_side_funding_crowded":
        value = _decimal(telemetry.get("direction_signed_funding_rate"))
        return value >= Decimal("0.0005") if value is not None else None

    if factor == "trend_5m_15m_full_alignment":
        return _known_all(_alignment(sample, "5"), _alignment(sample, "15"))
    if factor == "trend_15m_60m_full_alignment":
        return _known_all(_alignment(sample, "15"), _alignment(sample, "60"))
    if factor == "trend_all_timeframes_full_alignment":
        return _known_all(
            _alignment(sample, "5"),
            _alignment(sample, "15"),
            _alignment(sample, "60"),
        )
    if factor == "regime_5m_15m_trend":
        return _known_all(
            _frame_regime_trend(sample, "5"),
            _frame_regime_trend(sample, "15"),
        )
    if factor == "orderflow_both_aligned":
        return _known_all(
            _factor_value(sample, "taker_flow_aligned"),
            _factor_value(sample, "orderbook_flow_aligned"),
        )
    if factor == "trend_15m_and_orderflow_both_aligned":
        return _known_all(
            _alignment(sample, "15"),
            _factor_value(sample, "orderflow_both_aligned"),
        )
    if factor == "trend_15m_and_low_spread":
        return _known_all(
            _alignment(sample, "15"),
            _factor_value(sample, "spread_le_5bps"),
        )
    if factor == "trend_15m_with_3m_pullback":
        trend = _alignment(sample, "15")
        move = _factor_value(sample, "move_3m_aligned")
        if trend is None or move is None:
            return None
        return trend and not move
    raise ValueError(f"unknown research factor: {factor}")


SINGLE_FACTORS = (
    "trend_5m_full_alignment",
    "trend_15m_full_alignment",
    "trend_60m_full_alignment",
    "adx_5m_ge_20",
    "adx_15m_ge_20",
    "adx_60m_ge_20",
    "momentum_5m_ge_1atr",
    "momentum_15m_ge_1atr",
    "momentum_60m_ge_1atr",
    "regime_5m_trend",
    "regime_15m_trend",
    "regime_60m_trend",
    "atr_15m_expanding",
    "taker_flow_aligned",
    "orderbook_flow_aligned",
    "move_1m_aligned",
    "move_3m_aligned",
    "one_minute_displacement",
    "reclaim_or_retest_1m",
    "spread_le_5bps",
    "distance_le_5bps",
    "score_ge_60",
    "score_separation_ge_8",
    "context_aligned",
    "same_side_funding_crowded",
)

INTERACTION_FACTORS = (
    "trend_5m_15m_full_alignment",
    "trend_15m_60m_full_alignment",
    "trend_all_timeframes_full_alignment",
    "regime_5m_15m_trend",
    "orderflow_both_aligned",
    "trend_15m_and_orderflow_both_aligned",
    "trend_15m_and_low_spread",
    "trend_15m_with_3m_pullback",
)

FACTORS = (*SINGLE_FACTORS, *INTERACTION_FACTORS)


def _group_stats(samples: tuple[ResearchTradeSample, ...]) -> dict[str, object]:
    if not samples:
        return {"sample_size": 0}
    net_returns = tuple(item.net_return_bps for item in samples)
    price_returns = tuple(item.price_return_bps for item in samples)
    pnls = tuple(item.net_pnl for item in samples)
    positive = sum(value > 0 for value in pnls)
    gross_profit = sum((value for value in pnls if value > 0), Decimal(0))
    gross_loss = abs(sum((value for value in pnls if value < 0), Decimal(0)))
    mfe = tuple(item.mfe_r for item in samples if item.mfe_r is not None)
    mae = tuple(item.mae_r for item in samples if item.mae_r is not None)
    return {
        "sample_size": len(samples),
        "win_rate": str(Decimal(positive) / Decimal(len(samples))),
        "avg_price_return_bps": str(sum(price_returns, Decimal(0)) / Decimal(len(samples))),
        "avg_net_return_bps": str(sum(net_returns, Decimal(0)) / Decimal(len(samples))),
        "median_net_return_bps": str(median(net_returns)),
        "avg_net_pnl": str(sum(pnls, Decimal(0)) / Decimal(len(samples))),
        "profit_factor": str(gross_profit / gross_loss) if gross_loss > 0 else None,
        "median_mfe_r": str(median(mfe)) if mfe else None,
        "median_mae_r": str(median(mae)) if mae else None,
    }


def _effect(delta_net: Decimal | None) -> str | None:
    if delta_net is None:
        return None
    if delta_net > 0:
        return "SUPPORTIVE"
    if delta_net < 0:
        return "ADVERSE"
    return "NEUTRAL"


def analyze_factor_attribution(
    samples: tuple[ResearchTradeSample, ...],
) -> dict[str, object]:
    factors: dict[str, object] = {}
    ranked: list[tuple[Decimal, str]] = []
    for factor in FACTORS:
        true_group = tuple(item for item in samples if _factor_value(item, factor) is True)
        false_group = tuple(item for item in samples if _factor_value(item, factor) is False)
        true_stats = _group_stats(true_group)
        false_stats = _group_stats(false_group)
        delta_net: Decimal | None = None
        delta_price: Decimal | None = None
        if true_group and false_group:
            true_net = sum((item.net_return_bps for item in true_group), Decimal(0)) / Decimal(
                len(true_group)
            )
            false_net = sum((item.net_return_bps for item in false_group), Decimal(0)) / Decimal(
                len(false_group)
            )
            true_price = sum(
                (item.price_return_bps for item in true_group), Decimal(0)
            ) / Decimal(len(true_group))
            false_price = sum(
                (item.price_return_bps for item in false_group), Decimal(0)
            ) / Decimal(len(false_group))
            delta_net = true_net - false_net
            delta_price = true_price - false_price

        is_interaction = factor in INTERACTION_FACTORS
        minimum_samples = (
            MIN_INTERACTION_SAMPLES if is_interaction else MIN_ATTRIBUTION_SAMPLES
        )
        minimum_group_samples = (
            MIN_INTERACTION_GROUP_SAMPLES if is_interaction else MIN_GROUP_SAMPLES
        )
        rank_eligible = (
            len(samples) >= minimum_samples
            and len(true_group) >= minimum_group_samples
            and len(false_group) >= minimum_group_samples
            and delta_net is not None
        )
        if rank_eligible and delta_net is not None:
            ranked.append((abs(delta_net), factor))
        factors[factor] = {
            "kind": "INTERACTION" if is_interaction else "SINGLE",
            "true": true_stats,
            "false": false_stats,
            "missing": len(samples) - len(true_group) - len(false_group),
            "delta_net_return_bps": str(delta_net) if delta_net is not None else None,
            "delta_price_return_bps": str(delta_price) if delta_price is not None else None,
            "effect": _effect(delta_net),
            "minimum_samples_for_ranking": minimum_samples,
            "minimum_group_samples": minimum_group_samples,
            "rank_eligible": rank_eligible,
        }

    ranked.sort(reverse=True)
    strongest = ranked[0][1] if ranked else None
    strongest_effect = (
        factors[strongest]["effect"]
        if strongest is not None and isinstance(factors[strongest], dict)
        else None
    )
    if len(samples) < MIN_ATTRIBUTION_SAMPLES:
        status = "OBSERVE_ONLY"
    elif len(samples) < MIN_INTERACTION_SAMPLES:
        status = "PRELIMINARY_ATTRIBUTION"
    elif len(samples) < 200:
        status = "STRONGER_ATTRIBUTION"
    else:
        status = "SERIOUS_ATTRIBUTION"

    ranked_details = [
        {
            "factor": name,
            "kind": factors[name]["kind"],
            "effect": factors[name]["effect"],
            "delta_net_return_bps": factors[name]["delta_net_return_bps"],
            "delta_price_return_bps": factors[name]["delta_price_return_bps"],
        }
        for _, name in ranked
    ]
    return {
        "research_schema": RESEARCH_SCHEMA,
        "strategy_id": RESEARCH_STRATEGY_ID,
        "status": status,
        "sample_size": len(samples),
        "minimum_samples_for_ranking": MIN_ATTRIBUTION_SAMPLES,
        "minimum_group_samples": MIN_GROUP_SAMPLES,
        "minimum_interaction_samples_for_ranking": MIN_INTERACTION_SAMPLES,
        "minimum_interaction_group_samples": MIN_INTERACTION_GROUP_SAMPLES,
        "strongest_factor": strongest,
        "strongest_factor_effect": strongest_effect,
        "ranked_factors": [name for _, name in ranked],
        "ranked_factor_details": ranked_details,
        "overall": _group_stats(samples),
        "factors": factors,
        "sample_signal_ids": [item.signal_id for item in samples],
        "method": {
            "outcome_primary": "net_return_bps = net_pnl / entry_notional * 10000",
            "outcome_price": "direction-correct entry-to-exit price return in bps",
            "ranking": "absolute true-vs-false difference in mean net return bps",
            "single_factor_minimum": "50 total / 15 TRUE / 15 FALSE",
            "interaction_factor_minimum": "100 total / 25 TRUE / 25 FALSE",
            "hindsight_guard": "only entry-time telemetry tagged with research_schema is used",
            "no_automatic_strategy_change_below_50_samples": True,
        },
    }


def run_research_attribution() -> dict[str, object]:
    config = SupabasePersistenceConfig.from_environment()
    if not config.enabled:
        raise PersistenceError("research attribution requires dedicated Crypto Scanner Supabase")

    closed_rows = _fetch_rows(
        config,
        "closed_trades",
        {
            "select": (
                "signal_id,symbol,direction,entry_qty,average_entry_price,average_exit_price,"
                "net_pnl,mfe_r,mae_r,history_complete,calibration_eligible,exit_time_ms"
            ),
            "calibration_eligible": "eq.true",
            "history_complete": "eq.true",
            "order": "exit_time_ms.asc",
            "limit": "1000",
        },
    )
    runtime_rows = _fetch_rows(
        config,
        "runtime_state",
        {
            "select": "state_key,state,updated_at_ms",
            "order": "updated_at_ms.desc",
            "limit": "5000",
        },
    )
    samples = build_research_samples(closed_rows, runtime_rows)
    report = analyze_factor_attribution(samples)
    generated_at_ms = _now_ms()
    persisted = dict(report)
    persisted["generated_at_ms"] = generated_at_ms

    with SupabaseRestClient(config) as rest:
        rest.upsert(
            "runtime_state",
            (
                {
                    "state_key": RESEARCH_STATE_KEY,
                    "version": 1,
                    "state": persisted,
                    "updated_at_ms": generated_at_ms,
                },
            ),
            on_conflict=("state_key",),
        )
    return persisted


def main() -> None:
    print(json.dumps(run_research_attribution(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
