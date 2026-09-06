from __future__ import annotations

from decimal import Decimal

import httpx

from crypto_scanner.binance.microstructure import BinanceDemoMicrostructureClient


def test_microstructure_computes_depth_and_taker_pressure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/depth"):
            return httpx.Response(
                200,
                json={
                    "T": 1_000,
                    "bids": [["100", "2"], ["99", "1"]],
                    "asks": [["101", "1"]],
                },
            )
        if request.url.path.endswith("/trades"):
            return httpx.Response(
                200,
                json=[
                    {"qty": "3", "isBuyerMaker": False, "time": 1_100},
                    {"qty": "1", "isBuyerMaker": True, "time": 1_200},
                ],
            )
        raise AssertionError(request.url.path)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        micro = BinanceDemoMicrostructureClient(client=client)
        evidence = micro.get_evidence("btcusdt", depth_limit=20, trade_limit=2)

    assert evidence.symbol == "BTCUSDT"
    assert evidence.orderbook_imbalance == Decimal("0.5")
    assert evidence.taker_pressure == Decimal("0.5")
    assert evidence.depth_levels == 1
    assert evidence.trade_count == 2
    assert evidence.observed_at_ms >= 1_200
