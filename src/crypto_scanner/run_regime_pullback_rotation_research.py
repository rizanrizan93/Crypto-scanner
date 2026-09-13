from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from crypto_scanner.adaptive_d1_data import fetch_btc_d1
from crypto_scanner.adaptive_h4_data import fetch_funding_history, fetch_price_history
from crypto_scanner.adaptive_regime_signal import build_regimes
from crypto_scanner.regime_leader_laggard_core import resample_daily
from crypto_scanner.regime_leader_laggard_metrics import passes, summarize
from crypto_scanner.regime_pullback_rotation_core import build_trades

UNIVERSE = (
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
    "TRXUSDT", "SUIUSDT", "AAVEUSDT", "UNIUSDT", "ETCUSDT", "NEARUSDT",
    "ATOMUSDT", "XLMUSDT",
)


def load_symbol(symbol: str):
    h4, mp = fetch_price_history(symbol)
    funding, mf = fetch_funding_history(symbol)
    return symbol, h4, funding, mp, mf


def main() -> int:
    regimes = build_regimes(fetch_btc_d1())
    daily = {}
    funding = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(load_symbol, symbol): symbol for symbol in UNIVERSE}
        for future in as_completed(futures):
            symbol, h4, points, mp, mf = future.result()
            daily[symbol] = resample_daily(h4)
            funding[symbol] = points
            print("RPR_COVERAGE", symbol, len(h4), len(daily[symbol]), len(points), len(mp), len(mf))
    trades = build_trades(daily, funding, regimes)
    base = {p: summarize(trades, "base_return", p) for p in ("train", "validation", "oos")}
    stress = {p: summarize(trades, "stress_return", p) for p in ("train", "validation", "oos")}
    severe = {p: summarize(trades, "severe_return", p) for p in ("train", "validation", "oos")}
    passed = passes(stress, severe)
    report = {
        "schema_version": "CRYPTO_REGIME_PULLBACK_ROTATION_V1",
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "frozen_before_test": True,
        "direction_contract": "BULL long bottom3 7D dips; BEAR short top3 7D rallies; FLAT cash",
        "oos_used_for_selection": False,
        "historical_pass": ["RPR_7D_BOTTOM_TOP3_WEEKLY"] if passed else [],
        "rows": [{"candidate": "RPR_7D_BOTTOM_TOP3_WEEKLY", "base": base, "stress": stress, "severe_25bps": severe, "historical_pass": passed}],
    }
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
