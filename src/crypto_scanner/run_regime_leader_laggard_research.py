from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from crypto_scanner.adaptive_d1_data import fetch_btc_d1
from crypto_scanner.adaptive_h4_data import fetch_funding_history, fetch_price_history
from crypto_scanner.adaptive_regime_signal import build_regimes
from crypto_scanner.regime_leader_laggard_core import build_weekly_trades, resample_daily
from crypto_scanner.regime_leader_laggard_metrics import passes, summarize

UNIVERSE = (
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
    "TRXUSDT", "SUIUSDT", "AAVEUSDT", "UNIUSDT", "ETCUSDT", "NEARUSDT",
    "ATOMUSDT", "XLMUSDT",
)


def load_symbol(symbol: str):
    h4, missing_price = fetch_price_history(symbol)
    funding, missing_funding = fetch_funding_history(symbol)
    return symbol, h4, funding, missing_price, missing_funding


def main() -> int:
    regimes = build_regimes(fetch_btc_d1())
    daily = {}
    funding = {}
    coverage = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(load_symbol, symbol): symbol for symbol in UNIVERSE}
        for future in as_completed(futures):
            symbol, h4, points, missing_price, missing_funding = future.result()
            rows = resample_daily(h4)
            daily[symbol] = rows
            funding[symbol] = points
            coverage[symbol] = {
                "h4": len(h4),
                "d1": len(rows),
                "funding": len(points),
                "missing_price_months": len(missing_price),
                "missing_funding_months": len(missing_funding),
            }
            print("RLL_COVERAGE", symbol, coverage[symbol])

    trades = build_weekly_trades(daily, funding, regimes)
    base = {p: summarize(trades, "base_return", p) for p in ("train", "validation", "oos")}
    stress = {p: summarize(trades, "stress_return", p) for p in ("train", "validation", "oos")}
    severe = {p: summarize(trades, "severe_return", p) for p in ("train", "validation", "oos")}
    passed = passes(stress, severe)
    report = {
        "schema_version": "CRYPTO_REGIME_LEADER_LAGGARD_V1",
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "frozen_before_test": True,
        "direction_contract": "BULL long top3 28D leaders; BEAR short bottom3 28D laggards; FLAT cash",
        "oos_used_for_selection": False,
        "coverage": coverage,
        "historical_pass": ["RLL_28D_TOP_BOTTOM3_WEEKLY"] if passed else [],
        "rows": [{
            "candidate": "RLL_28D_TOP_BOTTOM3_WEEKLY",
            "base": base,
            "stress": stress,
            "severe_25bps": severe,
            "historical_pass": passed,
        }],
    }
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
