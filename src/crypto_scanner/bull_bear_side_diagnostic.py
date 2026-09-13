from __future__ import annotations

import json
from datetime import UTC, datetime
from math import sqrt
from statistics import mean, pstdev

from crypto_scanner.run_bull_bear_regime_research import _bounds, simulate


def side_summary(results, field: str, label: str, position: int):
    start, end = _bounds(label)
    rows = [r for r in results if r.position == position and start <= datetime.fromtimestamp(r.entry_time_ms / 1000, tz=UTC).date() <= end]
    values = [float(getattr(r, field)) for r in rows]
    equity = 1.0
    peak = 1.0
    dd = 0.0
    for value in values:
        equity *= max(0.0, 1.0 + value)
        peak = max(peak, equity)
        if peak > 0:
            dd = max(dd, (peak - equity) / peak)
    sigma = pstdev(values) if len(values) > 1 else 0.0
    return {
        "days": len(values),
        "total_return": equity - 1.0,
        "annualized_sharpe_active_days": 0.0 if sigma == 0 else sqrt(365.0) * mean(values) / sigma,
        "max_drawdown_active_sequence": dd,
        "avg_return": mean(values) if values else 0.0,
    }


def main() -> int:
    results = simulate()
    report = {"schema_version": "CRYPTO_BULL_BEAR_SIDE_ATTRIBUTION_V1", "diagnostic_only": True, "strategy_rules_unchanged": True, "stress": {}}
    for label in ("train", "validation", "oos"):
        report["stress"][label] = {
            "bull_long": side_summary(results, "stress_return", label, 1),
            "bear_short": side_summary(results, "stress_return", label, -1),
        }
    print("SIDE_ATTRIBUTION", json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
