from decimal import Decimal

import httpx
import pytest

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.models import InstrumentInfo
from crypto_scanner.binance.private_write import (
    BinanceTestnetOrderClient,
    build_split_protection_plan,
)
from crypto_scanner.execution_plan import EntryOrderPlan, ExecutionPlanError, TestnetExecutionArm


def _entry_plan(qty: str) -> EntryOrderPlan:
    amount = Decimal(qty)
    return EntryOrderPlan(
        signal_id="sig-1",
        order_link_id="cs-xrpusdt-1234567890abcdef",
        symbol="XRPUSDT",
        side="Buy",
        qty=amount,
        entry_price=Decimal("1.50"),
        stop_loss=Decimal("1.40"),
        take_profit_1=Decimal("2.00"),
        take_profit_2=Decimal("3.00"),
        risk_fraction=Decimal("0.001"),
        risk_amount=amount * Decimal("0.10"),
        notional=amount * Decimal("1.50"),
        leverage_equivalent=Decimal("0.01"),
    )


def _instrument(min_notional: str = "5") -> InstrumentInfo:
    return InstrumentInfo(
        symbol="XRPUSDT",
        status="Trading",
        contract_type="PERPETUAL",
        base_coin="XRP",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.0001"),
        min_order_qty=Decimal("0.1"),
        qty_step=Decimal("0.1"),
        min_notional_value=Decimal(min_notional),
        max_order_qty=None,
        max_market_order_qty=None,
        min_leverage=Decimal("1"),
        max_leverage=Decimal("50"),
        leverage_step=Decimal("1"),
    )


def test_small_position_falls_back_to_full_tp2() -> None:
    split = build_split_protection_plan(_entry_plan("3.6"), Decimal("3.6"), _instrument())
    assert split.take_profit_1 is None
    assert split.take_profit_2.qty == Decimal("3.6")
    assert split.stop_loss.qty == Decimal("3.6")


def test_large_position_splits_tp1_and_tp2_exactly() -> None:
    split = build_split_protection_plan(_entry_plan("10"), Decimal("10"), _instrument())
    assert split.take_profit_1 is not None
    assert split.take_profit_1.qty == Decimal("5")
    assert split.take_profit_2.qty == Decimal("5")
    assert split.take_profit_1.qty + split.take_profit_2.qty == split.full_qty
    assert split.stop_loss.qty == Decimal("10")


def test_cancel_algo_is_disarmed_before_network() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    writer = BinanceTestnetOrderClient(
        BinanceDemoCredentials(api_key="key", api_secret="secret"),
        TestnetExecutionArm(enabled=False),
        client=http,
        time_source_ms=lambda: 1000,
    )
    with pytest.raises(ExecutionPlanError):
        writer.cancel_algo_order(symbol="XRPUSDT", client_algo_id="cs-sl-test")
    assert calls == 0


def test_reduce_only_market_exit_sends_reduce_only_flag() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "orderId": 7,
                "clientOrderId": "cs-exit-123",
                "transactTime": 1001,
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    writer = BinanceTestnetOrderClient(
        BinanceDemoCredentials(api_key="key", api_secret="secret"),
        TestnetExecutionArm(enabled=True),
        client=http,
        time_source_ms=lambda: 1000,
    )
    ack = writer.submit_reduce_only_market_exit(
        symbol="XRPUSDT",
        exit_side="SELL",
        qty=Decimal("1.8"),
        client_order_id="cs-exit-123",
    )
    body = requests[0].content.decode()
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/fapi/v1/order"
    assert "reduceOnly=true" in body
    assert "positionSide=BOTH" in body
    assert "type=MARKET" in body
    assert ack.client_order_id == "cs-exit-123"
