from decimal import Decimal

import pytest

from crypto_scanner.bybit.public_ws import (
    BybitPublicWebSocketError,
    build_public_topics,
    parse_orderbook_update,
    parse_public_trades,
)


def test_public_topics_cover_ticker_trade_and_orderbook() -> None:
    topics = build_public_topics(["BTCUSDT", "ETHUSDT"], orderbook_depth=50)
    assert "tickers.BTCUSDT" in topics
    assert "publicTrade.BTCUSDT" in topics
    assert "orderbook.50.ETHUSDT" in topics
    assert len(topics) == 6


def test_public_trade_parser_preserves_taker_side() -> None:
    trades = parse_public_trades(
        {
            "topic": "publicTrade.BTCUSDT",
            "data": [
                {
                    "T": 1000,
                    "s": "BTCUSDT",
                    "S": "Buy",
                    "v": "0.25",
                    "p": "100",
                    "i": "trade-1",
                },
                {
                    "T": 1001,
                    "s": "BTCUSDT",
                    "S": "Sell",
                    "v": "0.10",
                    "p": "99.9",
                    "i": "trade-2",
                },
            ],
        }
    )
    assert trades[0].signed_size == Decimal("0.25")
    assert trades[1].signed_size == Decimal("-0.10")


def test_orderbook_parser_keeps_update_id_sequence_and_cts() -> None:
    update = parse_orderbook_update(
        {
            "topic": "orderbook.50.BTCUSDT",
            "type": "snapshot",
            "ts": 2000,
            "cts": 1999,
            "data": {
                "s": "BTCUSDT",
                "b": [["100", "2"]],
                "a": [["101", "3"]],
                "u": 12,
                "seq": 50,
            },
        }
    )
    assert update is not None
    assert update.update_id == 12
    assert update.sequence == 50
    assert update.engine_timestamp_ms == 1999
    assert update.bids[0].price == Decimal("100")


def test_orderbook_without_update_id_fails_closed() -> None:
    with pytest.raises(BybitPublicWebSocketError):
        parse_orderbook_update(
            {
                "topic": "orderbook.50.BTCUSDT",
                "type": "snapshot",
                "data": {"s": "BTCUSDT", "b": [], "a": []},
            }
        )
