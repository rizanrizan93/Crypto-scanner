from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any

import httpx

from crypto_scanner.hot_watch import RESEARCH_SCHEMA, RESEARCH_STRATEGY_ID
from crypto_scanner.persistence import (
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseRestClient,
)

RESEARCH_STATE_KEY = "research:factor_attribution:v1"
MIN_ATTRIBUTION_SAMPLES = 20
MIN_GROUP_SAMPLES = 5


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


def _factor_value(sample: ResearchTradeSample, factor: str) -> bool | None:
    telemetry = sample.telemetry
    if factor in {"trend_15m_full_alignment", "trend_60m_full_alignment"}:
        timeframe = "15" if "15m" in factor else "60"
        frame = _frame(sample, timeframe)
        if frame is None:
            return None
        count = frame.get("alignment_count")
        return int(count) == 3 if count is not None else None
    if factor in {"adx_15m_ge_20", "adx_60m_ge_20"}:
        timeframe = "15" if "15m" in factor else "60"
        frame = _frame(sample, timeframe)
        value = _decimal(frame.get("adx14")) if frame else None
        return value >= Decimal(20) if value is not None else None
    if factor in {"momentum_15m_ge_1atr", "momentum_60m_ge_1atr"}:
        timeframe = "15" if "15m" in factor else "60"
        frame = _frame(sample, timeframe)
        value = (
            _decimal(frame.get("direction_signed_momentum_to_atr")) if frame else None
        )
        return value >= Decimal(1) if value is not None else None
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
    raise ValueError(f"unknown research factor: {factor}")


FACTORS = (
    "trend_15m_full_alignment",
    "trend_60m_full_alignment",
    "adx_15m_ge_20",
    "adx_60m_ge_20",
    "momentum_15m_ge_1atr",
    "momentum_60m_ge_1atr",
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

        rank_eligible = (
            len(samples) >= MIN_ATTRIBUTION_SAMPLES
            and len(true_group) >= MIN_GROUP_SAMPLES
            and len(false_group) >= MIN_GROUP_SAMPLES
            and delta_net is not None
        )
        if rank_eligible and delta_net is not None:
            ranked.append((abs(delta_net), factor))
        factors[factor] = {
            "true": true_stats,
            "false": false_stats,
            "missing": len(samples) - len(true_group) - len(false_group),
            "delta_net_return_bps": str(delta_net) if delta_net is not None else None,
            "delta_price_return_bps": str(delta_price) if delta_price is not None else None,
            "rank_eligible": rank_eligible,
        }

    ranked.sort(reverse=True)
    strongest = ranked[0][1] if ranked else None
    if len(samples) < MIN_ATTRIBUTION_SAMPLES:
        status = "OBSERVE_ONLY"
    elif len(samples) < 50:
        status = "PRELIMINARY_ATTRIBUTION"
    elif len(samples) < 100:
        status = "STRONGER_ATTRIBUTION"
    else:
        status = "SERIOUS_ATTRIBUTION"

    return {
        "research_schema": RESEARCH_SCHEMA,
        "strategy_id": RESEARCH_STRATEGY_ID,
        "status": status,
        "sample_size": len(samples),
        "minimum_samples_for_ranking": MIN_ATTRIBUTION_SAMPLES,
        "minimum_group_samples": MIN_GROUP_SAMPLES,
        "strongest_factor": strongest,
        "ranked_factors": [name for _, name in ranked],
        "overall": _group_stats(samples),
        "factors": factors,
        "sample_signal_ids": [item.signal_id for item in samples],
        "method": {
            "outcome_primary": "net_return_bps = net_pnl / entry_notional * 10000",
            "outcome_price": "direction-correct entry-to-exit price return in bps",
            "ranking": "absolute true-vs-false difference in mean net return bps",
            "hindsight_guard": "only entry-time telemetry tagged with research_schema is used",
            "no_automatic_strategy_change_below_20_samples": True,
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
