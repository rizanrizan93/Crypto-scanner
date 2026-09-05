from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from crypto_scanner.bybit.public_rest import BybitPublicRestClient
from crypto_scanner.bybit.public_ws import BybitPublicWebSocket, parse_orderbook_update
from crypto_scanner.config import load_runtime_config
from crypto_scanner.market_state import LocalOrderBook


def run_rest_smoke() -> dict[str, Any]:
    config = load_runtime_config()
    report: dict[str, Any] = {"venue": "BYBIT", "environment": "TESTNET", "symbols": {}}

    with BybitPublicRestClient(base_url=config.bybit_rest_url) as client:
        for symbol in config.universe:
            instrument = client.get_instrument(symbol)
            ticker = client.get_ticker(symbol)
            if instrument.status != "Trading":
                raise RuntimeError(f"{symbol} is not Trading on Bybit Testnet")
            if instrument.settle_coin != "USDT":
                raise RuntimeError(f"{symbol} is not a USDT-settled instrument")
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


async def run_websocket_smoke(seconds: float) -> dict[str, Any]:
    config = load_runtime_config()
    symbol = config.universe[0]
    stream = BybitPublicWebSocket((symbol,), url=config.bybit_public_ws_url, orderbook_depth=50)
    book = LocalOrderBook(symbol)
    seen: set[str] = set()

    try:
        async with asyncio.timeout(seconds):
            async for message in stream.messages():
                topic = str(message.get("topic", ""))
                if topic.startswith("tickers."):
                    seen.add("ticker")
                elif topic.startswith("publicTrade."):
                    seen.add("trade")
                elif topic.startswith("orderbook."):
                    update = parse_orderbook_update(message)
                    if update is not None:
                        book.apply(update)
                        seen.add("orderbook")
                if {"ticker", "orderbook"}.issubset(seen):
                    break
    except TimeoutError:
        pass

    required = {"ticker", "orderbook"}
    if not required.issubset(seen):
        missing = sorted(required - seen)
        raise RuntimeError(f"Bybit Testnet public WebSocket smoke missing: {missing}")

    return {
        "symbol": symbol,
        "seen": sorted(seen),
        "best_bid": str(book.best_bid),
        "best_ask": str(book.best_ask),
        "spread_bps": str(book.spread_bps),
        "book_imbalance_10": str(book.imbalance(depth=10)),
        "last_update_id": book.last_update_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test Bybit Testnet public market data")
    parser.add_argument("--websocket-seconds", type=float, default=10.0)
    args = parser.parse_args()

    report = run_rest_smoke()
    report["websocket"] = asyncio.run(run_websocket_smoke(args.websocket_seconds))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
