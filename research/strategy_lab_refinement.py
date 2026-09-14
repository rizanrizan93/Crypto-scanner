# ruff: noqa
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import httpx

from strategy_lab_all import (
    BASE_COST_BPS,
    BINANCE_ARCHIVE,
    END,
    START,
    TRAIN_END,
    UNIVERSE,
    VALIDATION_END,
    Market,
    _checked_zip,
    backtest,
    corr,
    funding_dislocation_weights,
    load_all_data,
    metrics,
    regime_specialist_weights,
    trend_weights,
)


def scale_weights(weights: dict[date, dict[str, float]], scale: float):
    return {d: {s: w * scale for s, w in row.items()} for d, row in weights.items()}


def blend_weights(parts: list[tuple[float, dict[date, dict[str, float]]]]):
    days = sorted({d for _, rows in parts for d in rows})
    out = {}
    for d in days:
        row = {}
        for coefficient, rows in parts:
            for s, w in rows.get(d, {}).items():
                row[s] = row.get(s, 0.0) + coefficient * w
        out[d] = {s: w for s, w in row.items() if abs(w) > 1e-12}
    return out


def _fixed_weight_trailing_vol(m: Market, weights: dict[str, float], signal: date, lookback: int):
    idx = m.calendar.index(signal) if signal in m.calendar else -1
    if idx < lookback:
        return None
    returns = []
    for pos in range(idx - lookback + 1, idx + 1):
        d1, d0 = m.calendar[pos], m.calendar[pos - 1]
        total = 0.0
        observed = False
        for s, w in weights.items():
            b0, b1 = m.bar(s, d0), m.bar(s, d1)
            if b0 is None or b1 is None or b0.close <= 0:
                continue
            total += w * (b1.close / b0.close - 1)
            observed = True
        if observed:
            returns.append(total)
    if len(returns) < max(30, lookback // 2):
        return None
    sd = statistics.pstdev(returns)
    return sd * math.sqrt(365) if sd > 1e-12 else None


def vol_target_weights(
    m: Market,
    base: dict[date, dict[str, float]],
    target_ann_vol: float,
    *,
    lookback: int = 60,
    max_gross: float = 1.0,
):
    out = {}
    for i, d in enumerate(m.calendar):
        if i == 0:
            continue
        row = base.get(d, {})
        if not row:
            out[d] = {}
            continue
        signal = m.calendar[i - 1]
        observed = _fixed_weight_trailing_vol(m, row, signal, lookback)
        gross = sum(abs(w) for w in row.values())
        if observed is None or observed <= 0 or gross <= 0:
            out[d] = {}
            continue
        scale = min(1.0, target_ann_vol / observed, max_gross / gross)
        out[d] = {s: w * scale for s, w in row.items()}
    return out


def _metric_url(symbol: str, d: date):
    name = f"{symbol}-metrics-{d.isoformat()}.zip"
    return f"{BINANCE_ARCHIVE}/daily/metrics/{symbol}/{name}"


def _metric_value(client: httpx.Client, symbol: str, d: date):
    payload = _checked_zip(client, _metric_url(symbol, d))
    if payload is None:
        return None
    import zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if len(names) != 1:
            return None
        raw = zf.read(names[0]).decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(raw)))
    if not rows:
        return None
    row = rows[-1]
    raw_value = row.get("sum_open_interest_value") or row.get("sum_open_interest")
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def fetch_candidate_oi(
    m: Market,
    base: dict[date, dict[str, float]],
):
    requests = set()
    day_pairs = {}
    for i, d in enumerate(m.calendar):
        if i < 2 or not base.get(d):
            continue
        signal, prior = m.calendar[i - 1], m.calendar[i - 2]
        for symbol in base[d]:
            requests.add((symbol, signal))
            requests.add((symbol, prior))
            day_pairs[(d, symbol)] = (signal, prior)

    values = {}
    def fetch_one(item):
        symbol, d = item
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            return item, _metric_value(client, symbol, d)

    with ThreadPoolExecutor(max_workers=24) as pool:
        jobs = [pool.submit(fetch_one, item) for item in sorted(requests)]
        for fut in as_completed(jobs):
            item, value = fut.result()
            values[item] = value

    growth = {}
    for key, (signal, prior) in day_pairs.items():
        d, symbol = key
        current = values.get((symbol, signal))
        previous = values.get((symbol, prior))
        if current is not None and previous is not None and previous > 0:
            growth[key] = current / previous - 1
    return growth, len(requests), sum(v is not None for v in values.values())


def oi_filter(base: dict[date, dict[str, float]], growth: dict[tuple[date, str], float], threshold: float):
    out = {}
    for d, row in base.items():
        filtered = {s: w for s, w in row.items() if growth.get((d, s), -999.0) >= threshold}
        if filtered:
            source_gross = sum(abs(w) for w in row.values())
            filtered_gross = sum(abs(w) for w in filtered.values())
            if filtered_gross > 0:
                multiplier = source_gross / filtered_gross
                filtered = {s: w * multiplier for s, w in filtered.items()}
        out[d] = filtered
    return out


def summarize(m: Market, weights, baseline_returns):
    bt = backtest(m, weights, BASE_COST_BPS)
    return {
        "validation": metrics(bt.returns, TRAIN_END + timedelta(days=1), VALIDATION_END),
        "oos": metrics(bt.returns, VALIDATION_END + timedelta(days=1), END),
        "overall": metrics(bt.returns),
        "avg_exposure": statistics.fmean(bt.exposure.values()) if bt.exposure else 0.0,
        "annual_turnover": sum(bt.turnover.values()) / max(1, len(bt.turnover)) * 365,
        "oos_corr_current": corr(bt.returns, baseline_returns, VALIDATION_END + timedelta(days=1), END),
        "cost_stress": {
            str(cost): {
                "validation": metrics(backtest(m, weights, cost).returns, TRAIN_END + timedelta(days=1), VALIDATION_END),
                "oos": metrics(backtest(m, weights, cost).returns, VALIDATION_END + timedelta(days=1), END),
            }
            for cost in (8.0, 14.0, 25.0)
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="strategy_lab_refinement_results.json")
    args = parser.parse_args()

    bars, funding, _recent_oi, _cm, _bar_sources, _funding_sources = load_all_data()
    m = Market(bars, funding)

    current = regime_specialist_weights(m)
    vol20 = trend_weights(m, "vol20")
    vol30 = trend_weights(m, "vol30")
    funding_z2 = funding_dislocation_weights(m, 2.0)
    current_bt = backtest(m, current, BASE_COST_BPS)

    vol20_vt20 = vol_target_weights(m, vol20, 0.20)
    vol20_vt30 = vol_target_weights(m, vol20, 0.30)
    vol30_vt20 = vol_target_weights(m, vol30, 0.20)
    vol30_vt30 = vol_target_weights(m, vol30, 0.30)

    candidates = {
        "vol20_vt20": vol20_vt20,
        "vol20_vt30": vol20_vt30,
        "vol30_vt20": vol30_vt20,
        "vol30_vt30": vol30_vt30,
        "current80_vol30vt20_20": blend_weights([(0.80, current), (0.20, vol30_vt20)]),
        "current75_vol30vt20_25": blend_weights([(0.75, current), (0.25, vol30_vt20)]),
        "current80_vol20vt20_20": blend_weights([(0.80, current), (0.20, vol20_vt20)]),
        "current90_fundingz2_10": blend_weights([(0.90, current), (0.10, funding_z2)]),
        "current70_vol30vt20_20_funding10": blend_weights(
            [(0.70, current), (0.20, vol30_vt20), (0.10, funding_z2)]
        ),
    }

    oi_growth, oi_requested, oi_available = fetch_candidate_oi(m, funding_z2)
    for threshold in (0.0, 0.02, 0.05):
        candidates[f"fundingz2_oi_growth_{int(threshold * 100):02d}pct"] = oi_filter(
            funding_z2, oi_growth, threshold
        )

    report = {
        "research_only": True,
        "live_execution_mutated": False,
        "forward_demo_promotion_mutated": False,
        "method": {
            "base_cost_bps": BASE_COST_BPS,
            "validation": "2023-01-01/2024-12-31",
            "oos": "2025-01-01/2026-09-12",
            "vol_target_lookback_days": 60,
            "vol_targets": [0.20, 0.30],
            "oi_archive": "BINANCE_VISION_USDM_DAILY_METRICS_CHECKSUM_VERIFIED",
            "oi_rule": "retain funding-z2 legs only when prior-close OI value growth meets threshold",
        },
        "oi_archive_coverage": {
            "requested_files": oi_requested,
            "available_files": oi_available,
            "usable_growth_observations": len(oi_growth),
        },
        "baseline_current": summarize(m, current, current_bt.returns),
        "candidates": {
            name: summarize(m, weights, current_bt.returns)
            for name, weights in candidates.items()
        },
    }

    report["two_window_pass"] = [
        name
        for name, row in report["candidates"].items()
        if row["validation"]["cagr"] > 0
        and row["validation"]["sharpe"] > 0
        and row["validation"]["max_dd"] > -0.40
        and row["oos"]["cagr"] > 0
        and row["oos"]["sharpe"] > 0
        and row["oos"]["max_dd"] > -0.40
    ]

    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print("REFINEMENT_AUDIT=" + json.dumps(report, separators=(",", ":"), sort_keys=True))
    print(f"REFINEMENT_OUTPUT={args.output}")


if __name__ == "__main__":
    main()
