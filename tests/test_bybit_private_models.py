from decimal import Decimal

from crypto_scanner.bybit.private_models import (
    parse_order_snapshot,
    parse_position_snapshot,
)


def test_open_position_parses_exchange_state() -> None:
    position = parse_position_snapshot(
        {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "size": "0.01",
            "avgPrice": "60000",
            "positionValue": "600",
            "leverage": "2",
            "markPrice": "60500",
            "liqPrice": "30000",
            "unrealisedPnl": "5",
            "cumRealisedPnl": "1",
            "positionIM": "300",
            "positionMM": "3",
            "takeProfit": "62000",
            "stopLoss": "59000",
            "trailingStop": "",
            "updatedTime": "1700000000100",
        }
    )
    assert position.is_open is True
    assert position.size == Decimal("0.01")
    assert position.stop_loss == Decimal("59000")


def test_zero_position_is_not_open() -> None:
    position = parse_position_snapshot(
        {
            "symbol": "ETHUSDT",
            "side": "",
            "size": "0",
        }
    )
    assert position.is_open is False


def test_open_order_parses_execution_state() -> None:
    order = parse_order_snapshot(
        {
            "orderId": "order-1",
            "orderLinkId": "scanner-1",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "orderStatus": "New",
            "orderType": "Limit",
            "timeInForce": "GTC",
            "price": "60000",
            "qty": "0.01",
            "avgPrice": "",
            "leavesQty": "0.01",
            "cumExecQty": "0",
            "cumExecValue": "0",
            "cumExecFee": "0",
            "triggerPrice": "",
            "takeProfit": "62000",
            "stopLoss": "59000",
            "reduceOnly": False,
            "closeOnTrigger": False,
            "createdTime": "1700000000000",
            "updatedTime": "1700000000001",
        }
    )
    assert order.order_id == "order-1"
    assert order.qty == Decimal("0.01")
    assert order.take_profit == Decimal("62000")
    assert order.reduce_only is False
