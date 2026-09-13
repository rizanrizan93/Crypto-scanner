from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from crypto_scanner.funding_carry_research import FIXED_UNIVERSE
from crypto_scanner.run_funding_carry_research import (
    build_report,
    fetch_funding_history,
    fetch_price_history,
)

MAX_SYMBOL_WORKERS = 5


def _load_symbol(symbol: str):
    candles, missing_price = fetch_price_history(symbol)
    points, missing_funding = fetch_funding_history(symbol)
    return symbol, candles, missing_price, points, missing_funding


def main() -> int:
    prices = {}
    funding = {}
    unavailable_price = {}
    unavailable_funding = {}

    with ThreadPoolExecutor(max_workers=MAX_SYMBOL_WORKERS) as executor:
        futures = {
            executor.submit(_load_symbol, symbol): symbol
            for symbol in FIXED_UNIVERSE
        }
        for future in as_completed(futures):
            symbol, candles, missing_price, points, missing_funding = future.result()
            prices[symbol] = candles
            funding[symbol] = points
            unavailable_price[symbol] = missing_price
            unavailable_funding[symbol] = missing_funding
            print(
                f"FUNDING_COVERAGE {symbol} price_candles={len(candles)} "
                f"funding_points={len(points)} "
                f"missing_price_months={len(missing_price)} "
                f"missing_funding_months={len(missing_funding)}"
            )

    report = build_report(
        prices,
        funding,
        unavailable_price,
        unavailable_funding,
    )
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
