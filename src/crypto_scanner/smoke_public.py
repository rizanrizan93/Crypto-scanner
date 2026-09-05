from __future__ import annotations

import json
from typing import Any

from crypto_scanner.binance.public_rest import BinanceDemoPublicRestClient
from crypto_scanner.config import load_runtime_config


def run_rest_smoke() -> dict[str, Any]:
    config = load_runtime_config()
    report: dict[str, Any] = {"venue": "BINANCE", "environment": "DEMO", "symbols": {}}
    with BinanceDemoPublicRestClient(base_url=config.binance_rest_url) as client:
        for symbol in config.universe:
            instrument = client.get_instrument(symbol)
            ticker = client.get_ticker(symbol)
            if instrument.status != "Trading":
                raise RuntimeError(f"{symbol} is not Trading on Binance Futures Demo")
            if instrument.settle_coin != "USDT":
                raise RuntimeError(f"{symbol} is not a USDT-margined instrument")
            if ticker.ask_price <= ticker.bid_price:
                raise RuntimeError(f"{symbol} has invalid best bid/ask")
            report["symbols"][symbol] = {
                "contract_type": instrument.contract_type,
                "tick_size": str(instrument.tick_size),
                "qty_step": str(instrument.qty_step),
                "min_order_qty": str(instrument.min_order_qty),
                "last_price": str(ticker.last_price),
                "mark_price": str(ticker.mark_price),
                "index_price": str(ticker.index_price),
                "spread_bps": str(ticker.spread_bps),
                "open_interest": (
                    str(ticker.open_interest) if ticker.open_interest is not None else None
                ),
                "funding_rate": (
                    str(ticker.funding_rate) if ticker.funding_rate is not None else None
                ),
            }
        report["btc_klines"] = {
            interval: len(client.get_klines("BTCUSDT", interval, limit=20))
            for interval in ("1", "3", "5", "15", "60", "240")
        }
        report["btc_open_interest_points"] = len(
            client.get_open_interest("BTCUSDT", interval_time="5min", limit=10)
        )
        report["btc_funding_points"] = len(client.get_funding_history("BTCUSDT", limit=10))
    return report


def main() -> None:
    print(json.dumps(run_rest_smoke(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
