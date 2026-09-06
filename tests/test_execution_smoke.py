from dataclasses import replace
from decimal import Decimal

import pytest

from crypto_scanner.binance.models import InstrumentInfo, TickerSnapshot, WalletSnapshot
from crypto_scanner.execution_smoke import ExecutionSmokeError, build_smoke_plan


def _instrument() -> InstrumentInfo:
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
        min_notional_value=Decimal("5"),
        max_order_qty=Decimal("1000000"),
        max_market_order_qty=Decimal("1000000"),
        min_leverage=None,
        max_leverage=None,
        leverage_step=None,
    )


def _ticker() -> TickerSnapshot:
    return TickerSnapshot(
        symbol="XRPUSDT",
        last_price=Decimal("1.4"),
        mark_price=Decimal("1.4"),
        index_price=Decimal("1.4"),
        bid_price=Decimal("1.3999"),
        ask_price=Decimal("1.4001"),
        bid_size=Decimal("1000"),
        ask_size=Decimal("1000"),
        volume_24h=None,
        turnover_24h=None,
        open_interest=None,
        open_interest_value=None,
        funding_rate=None,
        next_funding_time_ms=None,
    )


def _wallet(equity: str = "5000") -> WalletSnapshot:
    value = Decimal(equity)
    return WalletSnapshot(
        account_type="FUTURES_DEMO",
        total_equity=value,
        total_wallet_balance=value,
        total_margin_balance=value,
        total_available_balance=value,
        total_perp_upl=Decimal(0),
        coins=(),
    )


def test_smoke_plan_is_minimum_notional_and_tiny_risk() -> None:
    plan = build_smoke_plan(
        instrument=_instrument(),
        ticker=_ticker(),
        wallet=_wallet(),
        smoke_id="run-1",
    )
    assert Decimal("5") <= plan.notional <= Decimal("10")
    assert plan.qty % Decimal("0.1") == 0
    assert plan.stop_loss < plan.entry_price < plan.take_profit_1 < plan.take_profit_2
    assert plan.risk_fraction <= Decimal("0.001")


def test_smoke_plan_rejects_oversized_minimum_notional() -> None:
    instrument = replace(_instrument(), min_notional_value=Decimal("50"))
    with pytest.raises(ExecutionSmokeError, match="exceeds 10"):
        build_smoke_plan(
            instrument=instrument,
            ticker=_ticker(),
            wallet=_wallet(),
            smoke_id="run-2",
        )
