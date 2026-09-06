from decimal import Decimal

import pytest

from crypto_scanner.binance.private_rest import IncomeRecord, UserTradeFill
from crypto_scanner.closed_trades import (
    ClosedTradeError,
    TradeDirection,
    reconstruct_closed_trades,
)


def _fill(
    trade_id: str,
    side: str,
    qty: str,
    price: str,
    realized: str,
    commission: str,
    time_ms: int,
) -> UserTradeFill:
    amount = Decimal(qty)
    px = Decimal(price)
    return UserTradeFill(
        symbol="XRPUSDT",
        trade_id=trade_id,
        order_id=f"order-{trade_id}",
        side=side,
        position_side="BOTH",
        price=px,
        qty=amount,
        quote_qty=amount * px,
        realized_pnl=Decimal(realized),
        commission=Decimal(commission),
        commission_asset="USDT",
        buyer=side == "BUY",
        maker=False,
        time_ms=time_ms,
    )


def test_long_flat_to_flat_reconstructs_net_pnl() -> None:
    fills = (
        _fill("1", "BUY", "3.6", "1.4000", "0", "0.0020", 1000),
        _fill("2", "SELL", "3.6", "1.4400", "0.144", "0.0021", 2000),
    )
    income = (
        IncomeRecord(
            symbol="XRPUSDT",
            income_type="FUNDING_FEE",
            income=Decimal("-0.001"),
            asset="USDT",
            time_ms=1500,
            transaction_id="f1",
            trade_id="",
            info="FUNDING_FEE",
        ),
    )
    result = reconstruct_closed_trades(fills, income)
    assert len(result) == 1
    trade = result[0]
    assert trade.direction is TradeDirection.LONG
    assert trade.average_entry_price == Decimal("1.4000")
    assert trade.average_exit_price == Decimal("1.4400")
    assert trade.realized_pnl == Decimal("0.144")
    assert trade.commission == Decimal("0.0041")
    assert trade.funding_fee == Decimal("-0.001")
    assert trade.net_pnl == Decimal("0.1389")
    assert trade.holding_time_ms == 1000


def test_partial_exit_is_reconstructed_until_flat() -> None:
    fills = (
        _fill("1", "BUY", "4", "1.40", "0", "0.002", 1000),
        _fill("2", "SELL", "1", "1.42", "0.02", "0.001", 1500),
        _fill("3", "SELL", "3", "1.44", "0.12", "0.002", 2000),
    )
    trade = reconstruct_closed_trades(fills)[0]
    assert trade.entry_qty == Decimal("4")
    assert trade.exit_qty == Decimal("4")
    assert trade.average_exit_price == Decimal("1.435")


def test_reversal_without_flat_state_fails_closed() -> None:
    fills = (
        _fill("1", "BUY", "1", "1.40", "0", "0", 1000),
        _fill("2", "SELL", "2", "1.41", "0.01", "0", 2000),
    )
    with pytest.raises(ClosedTradeError, match="reverses"):
        reconstruct_closed_trades(fills)
