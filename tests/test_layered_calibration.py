from __future__ import annotations

from decimal import Decimal

from crypto_scanner.binance.models import Candle, PositionSnapshot
from crypto_scanner.binance.private_rest import UserTradeFill
from crypto_scanner.closed_trades import TradeDirection
from crypto_scanner.phase7_audit import _remove_layered_r_metrics
from crypto_scanner.trajectory import (
    OpenEpisodeEvidence,
    infer_open_episode,
    reconstruct_conservative_trajectory,
)


def _fill(
    trade_id: str,
    order_id: str,
    side: str,
    qty: str,
    time_ms: int,
) -> UserTradeFill:
    quantity = Decimal(qty)
    price = Decimal("100")
    return UserTradeFill(
        symbol="XRPUSDT",
        trade_id=trade_id,
        order_id=order_id,
        side=side,
        position_side="BOTH",
        price=price,
        qty=quantity,
        quote_qty=quantity * price,
        realized_pnl=Decimal(0),
        commission=Decimal(0),
        commission_asset="USDT",
        buyer=side == "BUY",
        maker=False,
        time_ms=time_ms,
    )


def _position(size: str) -> PositionSnapshot:
    return PositionSnapshot(
        symbol="XRPUSDT",
        side="Buy",
        size=Decimal(size),
        avg_price=Decimal("100"),
        position_value=None,
        leverage=Decimal("1"),
        mark_price=Decimal("102"),
        liq_price=None,
        unrealised_pnl=None,
        cum_realised_pnl=None,
        position_im=None,
        position_mm=None,
        take_profit=None,
        stop_loss=None,
        trailing_stop=None,
        updated_time_ms=200_000,
    )


def test_open_episode_detects_multiple_distinct_entry_orders() -> None:
    episode = infer_open_episode(
        _position("3"),
        (
            _fill("1", "entry-a", "BUY", "2", 1000),
            _fill("2", "entry-b", "BUY", "1", 2000),
        ),
    )
    assert episode.entry_order_ids == ("entry-a", "entry-b")
    assert episode.layered_entry


def test_open_episode_does_not_misclassify_split_fill_of_one_order() -> None:
    episode = infer_open_episode(
        _position("3"),
        (
            _fill("1", "entry-a", "BUY", "1", 1000),
            _fill("2", "entry-a", "BUY", "2", 1001),
        ),
    )
    assert episode.entry_order_ids == ("entry-a",)
    assert not episode.layered_entry


def test_layered_calibration_keeps_geometry_but_nulls_r_metrics() -> None:
    episode = OpenEpisodeEvidence(
        symbol="XRPUSDT",
        direction=TradeDirection.LONG,
        entry_time_ms=0,
        entry_price=Decimal("100"),
        current_qty=Decimal("3"),
        trade_ids=("1", "2"),
        entry_order_ids=("entry-a", "entry-b"),
    )
    candle = Candle(
        start_time_ms=0,
        open=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("98"),
        close=Decimal("103"),
        volume=Decimal("10"),
        turnover=Decimal("1000"),
    )
    metrics = reconstruct_conservative_trajectory(
        episode,
        (candle,),
        measured_until_ms=60_000,
        current_price=Decimal("103"),
        initial_stop_loss=Decimal("95"),
    )
    assert metrics.mfe_r == Decimal("0.8")
    assert metrics.mae_r == Decimal("0.4")

    bounded = _remove_layered_r_metrics(metrics)
    assert bounded.initial_stop_loss == Decimal("95")
    assert bounded.mfe_r is None
    assert bounded.mae_r is None
    assert bounded.current_price == metrics.current_price
