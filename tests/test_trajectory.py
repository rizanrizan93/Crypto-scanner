from __future__ import annotations

import json
from decimal import Decimal

import pytest

from crypto_scanner.binance.models import Candle, PositionSnapshot
from crypto_scanner.binance.private_rest import UserTradeFill
from crypto_scanner.closed_trades import TradeDirection
from crypto_scanner.trajectory import (
    OpenEpisodeEvidence,
    TrajectoryError,
    TrajectoryQuality,
    infer_open_episode,
    reconstruct_conservative_trajectory,
)
from crypto_scanner.trajectory_store import JsonTrajectoryStore, TrajectoryRecord


def _position(*, side: str = "Buy", size: str = "1.5") -> PositionSnapshot:
    return PositionSnapshot(
        symbol="XRPUSDT",
        side=side,
        size=Decimal(size),
        avg_price=Decimal("100"),
        position_value=None,
        leverage=Decimal("1"),
        mark_price=Decimal("104"),
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


def _fill(
    trade_id: str,
    side: str,
    qty: str,
    time_ms: int,
    *,
    price: str = "100",
) -> UserTradeFill:
    quantity = Decimal(qty)
    trade_price = Decimal(price)
    return UserTradeFill(
        symbol="XRPUSDT",
        trade_id=trade_id,
        order_id=f"o-{trade_id}",
        side=side,
        position_side="BOTH",
        price=trade_price,
        qty=quantity,
        quote_qty=quantity * trade_price,
        realized_pnl=Decimal(0),
        commission=Decimal("0.001"),
        commission_asset="USDT",
        buyer=side == "BUY",
        maker=False,
        time_ms=time_ms,
    )


def _candle(start: int, high: str, low: str, close: str) -> Candle:
    return Candle(
        start_time_ms=start,
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10"),
        turnover=Decimal("1000"),
    )


def test_infer_open_episode_uses_latest_flat_to_open_window() -> None:
    fills = (
        _fill("1", "BUY", "1", 10_000),
        _fill("2", "SELL", "1", 20_000, price="101"),
        _fill("3", "BUY", "2", 120_000),
        _fill("4", "SELL", "0.5", 150_000, price="102"),
    )

    episode = infer_open_episode(_position(), fills)

    assert episode.entry_time_ms == 120_000
    assert episode.direction is TradeDirection.LONG
    assert episode.current_qty == Decimal("1.5")
    assert episode.trade_ids == ("3", "4")


def test_infer_open_episode_fails_if_fill_history_does_not_match_exchange() -> None:
    with pytest.raises(TrajectoryError, match="fill history ends"):
        infer_open_episode(_position(size="2"), (_fill("1", "BUY", "1", 10_000),))


def test_conservative_long_replay_excludes_partial_entry_and_current_minutes() -> None:
    episode = OpenEpisodeEvidence(
        symbol="XRPUSDT",
        direction=TradeDirection.LONG,
        entry_time_ms=30_000,
        entry_price=Decimal("100"),
        current_qty=Decimal("2"),
        trade_ids=("1",),
    )
    candles = (
        _candle(0, "120", "80", "100"),
        _candle(60_000, "105", "98", "103"),
        _candle(120_000, "110", "95", "106"),
        _candle(180_000, "130", "70", "104"),
    )

    metrics = reconstruct_conservative_trajectory(
        episode,
        candles,
        measured_until_ms=210_000,
        current_price=Decimal("104"),
        initial_stop_loss=Decimal("90"),
    )

    assert metrics.observation_count == 2
    assert metrics.favorable_extreme_price == Decimal("110")
    assert metrics.adverse_extreme_price == Decimal("95")
    assert metrics.mfe_per_unit == Decimal("10")
    assert metrics.mae_per_unit == Decimal("5")
    assert metrics.mfe_pct == Decimal("10")
    assert metrics.mae_pct == Decimal("5")
    assert metrics.mfe_r == Decimal("1")
    assert metrics.mae_r == Decimal("0.5")
    assert metrics.quality is TrajectoryQuality.CONSERVATIVE_1M_REPLAY
    assert metrics.history_complete is True


def test_conservative_short_replay_and_partial_history() -> None:
    episode = OpenEpisodeEvidence(
        symbol="XRPUSDT",
        direction=TradeDirection.SHORT,
        entry_time_ms=30_000,
        entry_price=Decimal("100"),
        current_qty=Decimal("1"),
        trade_ids=("1",),
    )
    candles = (_candle(120_000, "108", "90", "95"),)

    metrics = reconstruct_conservative_trajectory(
        episode,
        candles,
        measured_until_ms=210_000,
        current_price=Decimal("96"),
    )

    assert metrics.mfe_per_unit == Decimal("10")
    assert metrics.mae_per_unit == Decimal("8")
    assert metrics.current_pnl_per_unit == Decimal("4")
    assert metrics.quality is TrajectoryQuality.PARTIAL_HISTORY
    assert metrics.history_complete is False
    assert metrics.mfe_r is None
    assert metrics.mae_r is None


def test_invalid_initial_stop_is_rejected() -> None:
    episode = OpenEpisodeEvidence(
        symbol="XRPUSDT",
        direction=TradeDirection.LONG,
        entry_time_ms=0,
        entry_price=Decimal("100"),
        current_qty=Decimal("1"),
        trade_ids=("1",),
    )
    with pytest.raises(TrajectoryError, match="initial stop"):
        reconstruct_conservative_trajectory(
            episode,
            (),
            measured_until_ms=30_000,
            current_price=Decimal("101"),
            initial_stop_loss=Decimal("101"),
        )


def test_json_store_preserves_decimal_precision(tmp_path) -> None:
    episode = OpenEpisodeEvidence(
        symbol="XRPUSDT",
        direction=TradeDirection.LONG,
        entry_time_ms=0,
        entry_price=Decimal("1.23456789"),
        current_qty=Decimal("3.6"),
        trade_ids=("1",),
    )
    metrics = reconstruct_conservative_trajectory(
        episode,
        (),
        measured_until_ms=30_000,
        current_price=Decimal("1.24000001"),
    )
    path = tmp_path / "trajectory.json"
    JsonTrajectoryStore(path).save(
        (
            TrajectoryRecord(
                snapshot=metrics,
                calibration_eligible=False,
                persistence_mode="NO_SUPABASE",
                note="diagnostic",
            ),
        )
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload[0]["snapshot"]["entry_price"] == "1.23456789"
    assert payload[0]["snapshot"]["quality"] == "CONSERVATIVE_1M_REPLAY"
    assert payload[0]["calibration_eligible"] is False
