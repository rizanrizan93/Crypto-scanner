from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from crypto_scanner.adaptive_d1_data import fetch_btc_d1
from crypto_scanner.adaptive_h4_data import fetch_funding_history, fetch_price_history
from crypto_scanner.adaptive_regime_accounting import apply_costs
from crypto_scanner.adaptive_regime_backtest import simulate_price
from crypto_scanner.adaptive_regime_metrics import historical_pass, summarize
from crypto_scanner.adaptive_regime_signal import build_regimes, build_setups

UNIVERSE = (
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
    "TRXUSDT", "SUIUSDT", "AAVEUSDT", "UNIUSDT", "ETCUSDT", "NEARUSDT",
    "ATOMUSDT", "XLMUSDT",
)


def _label(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m")


def _check_gap(symbol: str, rows, missing: list[str], kind: str) -> None:
    if not rows:
        raise RuntimeError(f"NO_{kind}:{symbol}")
    first = _label(rows[0].start_time_ms if kind == "PRICE" else rows[0].funding_time_ms)
    bad = [item for item in missing if item >= first]
    if bad:
        raise RuntimeError(f"POST_LISTING_{kind}_GAP:{symbol}:{bad}")


def _load(symbol: str):
    prices, missing_price = fetch_price_history(symbol)
    funding, missing_funding = fetch_funding_history(symbol)
    _check_gap(symbol, prices, missing_price, "PRICE")
    _check_gap(symbol, funding, missing_funding, "FUNDING")
    return symbol, prices, funding, missing_price, missing_funding


def main() -> int:
    d1 = fetch_btc_d1()
    regimes = build_regimes(d1)
    print("ARR_REGIME", len(d1), len(regimes))
    market = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_load, symbol): symbol for symbol in UNIVERSE}
        for future in as_completed(futures):
            symbol, prices, funding, mp, mf = future.result()
            market[symbol] = (prices, funding)
            print("ARR_COVERAGE", symbol, len(prices), len(funding), len(mp), len(mf))

    all_trades = []
    setup_counts = {}
    for symbol in UNIVERSE:
        prices, funding = market[symbol]
        setups = build_setups(prices, regimes)
        setup_counts[symbol] = {
            "total": len(setups),
            "long": sum(x.direction > 0 for x in setups),
            "short": sum(x.direction < 0 for x in setups),
        }
        raw = simulate_price(symbol, prices, setups)
        all_trades.extend(apply_costs(raw, funding))
    trades = tuple(sorted(all_trades, key=lambda x: (x.entry_ms, x.symbol)))
    base = {p: summarize(trades, "base_r", p) for p in ("train", "validation", "oos")}
    stress = {p: summarize(trades, "stress_r", p) for p in ("train", "validation", "oos")}
    severe = {p: summarize(trades, "severe_r", p) for p in ("train", "validation", "oos")}
    passed = historical_pass(stress, severe)
    report = {
        "schema_version": "CRYPTO_ADAPTIVE_REGIME_ROUTER_V1",
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "frozen_before_test": True,
        "direction_contract": "BTC D1 bull routes LONG only; bear routes SHORT only; flat routes NO_TRADE",
        "oos_used_for_selection": False,
        "setup_counts": setup_counts,
        "historical_pass": ["ARR_BTC_D1_H4_PULLBACK_V1"] if passed else [],
        "rows": [{"candidate": "ARR_BTC_D1_H4_PULLBACK_V1", "base": base, "stress": stress, "severe_25bps": severe, "historical_pass": passed}],
    }
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
