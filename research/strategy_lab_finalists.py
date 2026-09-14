# ruff: noqa
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from datetime import date, timedelta
from pathlib import Path

from strategy_lab_all import (
    BASE_COST_BPS,
    END,
    TRAIN_END,
    UNIVERSE,
    VALIDATION_END,
    Market,
    _btc_regime,
    backtest,
    funding_dislocation_weights,
    load_all_data,
    metrics,
    regime_specialist_weights,
    trend_weights,
)
from strategy_lab_refinement import (
    blend_weights,
    fetch_candidate_oi,
    vol_target_weights,
)


def conservative_oi_filter(base, growth, threshold, max_symbol_abs=0.40):
    out = {}
    for d, row in base.items():
        kept = {}
        for s, w in row.items():
            if growth.get((d, s), -999.0) >= threshold:
                kept[s] = max(-max_symbol_abs, min(max_symbol_abs, w))
        out[d] = kept
    return out


def summarize(m, weights):
    bt = backtest(m, weights, BASE_COST_BPS)
    return {
        "validation": metrics(bt.returns, TRAIN_END + timedelta(days=1), VALIDATION_END),
        "oos": metrics(bt.returns, VALIDATION_END + timedelta(days=1), END),
        "cost_25bps": {
            "validation": metrics(backtest(m, weights, 25).returns, TRAIN_END + timedelta(days=1), VALIDATION_END),
            "oos": metrics(backtest(m, weights, 25).returns, VALIDATION_END + timedelta(days=1), END),
        },
        "cost_40bps": {
            "validation": metrics(backtest(m, weights, 40).returns, TRAIN_END + timedelta(days=1), VALIDATION_END),
            "oos": metrics(backtest(m, weights, 40).returns, VALIDATION_END + timedelta(days=1), END),
        },
        "avg_exposure": statistics.fmean(bt.exposure.values()) if bt.exposure else 0.0,
        "annual_turnover": sum(bt.turnover.values()) / max(1, len(bt.turnover)) * 365,
    }


def yearly(m, weights):
    bt = backtest(m, weights, BASE_COST_BPS)
    return {
        str(year): metrics(bt.returns, date(year, 1, 1), min(END, date(year, 12, 31)))
        for year in range(2021, 2027)
        if date(year, 1, 1) <= END
    }


def bucket_stats(values):
    if not values:
        return {"days": 0, "compound_return": 0.0, "sharpe": 0.0, "hit_rate": 0.0}
    eq = 1.0
    for r in values:
        eq *= 1 + max(r, -0.999999)
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values)
    return {
        "days": len(values),
        "compound_return": eq - 1,
        "sharpe": mean / sd * math.sqrt(365) if sd > 1e-12 else 0.0,
        "hit_rate": sum(r > 0 for r in values) / len(values),
    }


def regime_attribution(m, weights, start=date(2023, 1, 1)):
    bt = backtest(m, weights, BASE_COST_BPS)
    buckets = {"BULL": [], "BEAR": [], "TRANSITION": []}
    for i, d in enumerate(m.calendar):
        if i == 0 or d < start or d > END or d not in bt.returns:
            continue
        regime = _btc_regime(m, m.calendar[i - 1])
        buckets[regime].append(bt.returns[d])
    return {name: bucket_stats(vals) for name, vals in buckets.items()}


def symbol_contributions(m, weights, start=date(2023, 1, 1)):
    previous = {}
    contrib = {s: 0.0 for s in UNIVERSE}
    for i, d in enumerate(m.calendar):
        if i == 0 or d < start or d > END:
            continue
        d0 = m.calendar[i - 1]
        row = weights.get(d, {})
        keys = set(previous) | set(row)
        for s in keys:
            w = row.get(s, 0.0)
            b0, b1 = m.bar(s, d0), m.bar(s, d)
            price = 0.0
            if b0 is not None and b1 is not None and b0.close > 0:
                price = w * (b1.close / b0.close - 1)
            funding = -w * m.funding.get(s, {}).get(d, 0.0)
            turnover = abs(w - previous.get(s, 0.0))
            cost = turnover * BASE_COST_BPS / 10_000
            contrib[s] += price + funding - cost
        previous = dict(row)
    return dict(sorted(contrib.items(), key=lambda x: x[1], reverse=True))


def remove_symbol(weights, symbol):
    return {d: {s: w for s, w in row.items() if s != symbol} for d, row in weights.items()}


def leave_one_out(m, weights):
    full = backtest(m, weights, BASE_COST_BPS)
    full_oos = metrics(full.returns, VALIDATION_END + timedelta(days=1), END)
    rows = []
    active_symbols = sorted({s for row in weights.values() for s in row})
    for symbol in active_symbols:
        bt = backtest(m, remove_symbol(weights, symbol), BASE_COST_BPS)
        oos = metrics(bt.returns, VALIDATION_END + timedelta(days=1), END)
        rows.append({
            "symbol": symbol,
            "oos_cagr": oos["cagr"],
            "oos_sharpe": oos["sharpe"],
            "oos_max_dd": oos["max_dd"],
            "delta_sharpe_vs_full": oos["sharpe"] - full_oos["sharpe"],
        })
    rows.sort(key=lambda r: r["delta_sharpe_vs_full"])
    return rows


def _quantile(values, q):
    if not values:
        return 0.0
    rows = sorted(values)
    pos = q * (len(rows) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return rows[lo]
    frac = pos - lo
    return rows[lo] * (1 - frac) + rows[hi] * frac


def block_bootstrap(returns, *, seed=20260914, block=10, simulations=1500):
    vals = list(returns)
    if len(vals) < block * 4:
        return {"status": "INSUFFICIENT"}
    rng = random.Random(seed)
    cagr = []
    sharpe = []
    n = len(vals)
    years = n / 365.0
    for _ in range(simulations):
        sample = []
        while len(sample) < n:
            start = rng.randrange(0, n - block + 1)
            sample.extend(vals[start:start + block])
        sample = sample[:n]
        eq = 1.0
        for r in sample:
            eq *= 1 + max(r, -0.999999)
        cagr.append(eq ** (1 / years) - 1 if eq > 0 else -1.0)
        mean = statistics.fmean(sample)
        sd = statistics.pstdev(sample)
        sharpe.append(mean / sd * math.sqrt(365) if sd > 1e-12 else 0.0)
    return {
        "simulations": simulations,
        "block_days": block,
        "cagr_p05": _quantile(cagr, 0.05),
        "cagr_p50": _quantile(cagr, 0.50),
        "cagr_p95": _quantile(cagr, 0.95),
        "prob_cagr_positive": sum(x > 0 for x in cagr) / len(cagr),
        "sharpe_p05": _quantile(sharpe, 0.05),
        "sharpe_p50": _quantile(sharpe, 0.50),
        "prob_sharpe_positive": sum(x > 0 for x in sharpe) / len(sharpe),
    }


def bootstrap_window(m, weights, start=date(2023, 1, 1)):
    bt = backtest(m, weights, BASE_COST_BPS)
    vals = [r for d, r in sorted(bt.returns.items()) if start <= d <= END]
    return block_bootstrap(vals)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="strategy_lab_finalists_results.json")
    args = parser.parse_args()

    bars, funding, _oi, _cm, _bs, _fs = load_all_data()
    m = Market(bars, funding)
    current = regime_specialist_weights(m)
    vol30 = trend_weights(m, "vol30")
    vt20 = vol_target_weights(m, vol30, 0.20)
    vt30 = vol_target_weights(m, vol30, 0.30)
    fz2 = funding_dislocation_weights(m, 2.0)
    growth, requested, available = fetch_candidate_oi(m, fz2)
    oi0 = conservative_oi_filter(fz2, growth, 0.0)
    oi2 = conservative_oi_filter(fz2, growth, 0.02)

    candidates = {
        "current": current,
        "vol30_vt20": vt20,
        "vol30_vt30": vt30,
        "funding_oi0_conservative": oi0,
        "funding_oi2_conservative": oi2,
        "ensemble_conservative_70_20_10": blend_weights([(0.70, current), (0.20, vt20), (0.10, oi0)]),
        "ensemble_balanced_60_30_10": blend_weights([(0.60, current), (0.30, vt20), (0.10, oi0)]),
        "ensemble_growth_50_35_15": blend_weights([(0.50, current), (0.35, vt30), (0.15, oi0)]),
    }

    report = {
        "research_only": True,
        "promotion_mutated": False,
        "oi_coverage": {"requested": requested, "available": available, "growth_observations": len(growth)},
        "candidates": {},
    }
    for name, weights in candidates.items():
        bt = backtest(m, weights, BASE_COST_BPS)
        report["candidates"][name] = {
            "summary": summarize(m, weights),
            "yearly": yearly(m, weights),
            "regime_2023_2026": regime_attribution(m, weights),
            "symbol_contribution_2023_2026": symbol_contributions(m, weights),
            "bootstrap_2023_2026": bootstrap_window(m, weights),
        }
        if name in {"vol30_vt20", "vol30_vt30", "ensemble_balanced_60_30_10"}:
            report["candidates"][name]["leave_one_asset_out_oos"] = leave_one_out(m, weights)

    report["strict_pass"] = [
        name for name, row in report["candidates"].items()
        if row["summary"]["validation"]["cagr"] > 0
        and row["summary"]["validation"]["sharpe"] > 0.5
        and row["summary"]["validation"]["max_dd"] > -0.35
        and row["summary"]["oos"]["cagr"] > 0
        and row["summary"]["oos"]["sharpe"] > 0.5
        and row["summary"]["oos"]["max_dd"] > -0.25
        and row["summary"]["cost_25bps"]["validation"]["cagr"] > 0
        and row["summary"]["cost_25bps"]["oos"]["cagr"] > 0
    ]

    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    compact = {
        "oi_coverage": report["oi_coverage"],
        "strict_pass": report["strict_pass"],
        "finalists": {
            name: {
                "validation": row["summary"]["validation"],
                "oos": row["summary"]["oos"],
                "cost25_validation": row["summary"]["cost_25bps"]["validation"],
                "cost25_oos": row["summary"]["cost_25bps"]["oos"],
                "cost40_oos": row["summary"]["cost_40bps"]["oos"],
                "bootstrap": row["bootstrap_2023_2026"],
                "regime": row["regime_2023_2026"],
            }
            for name, row in report["candidates"].items()
        },
    }
    print("FINALISTS_AUDIT=" + json.dumps(compact, separators=(",", ":"), sort_keys=True))
    print(f"FINALISTS_OUTPUT={args.output}")


if __name__ == "__main__":
    main()
