from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from crypto_scanner.binance.models import Candle, PositionSnapshot
from crypto_scanner.binance.private_rest import UserTradeFill
from crypto_scanner.closed_trades import TradeDirection


_ONE_MINUTE_MS = 60_000


class TrajectoryError(RuntimeError):
    """Raised when a trajectory cannot be reconstructed without ambiguity."""


class TrajectoryQuality(StrEnum):
    CONSERVATIVE_1M_REPLAY = "CONSERVATIVE_1M_REPLAY"
    PARTIAL_HISTORY = "PARTIAL_HISTORY"


@dataclass(frozen=True, slots=True)
class OpenEpisodeEvidence:
    symbol: str
    direction: TradeDirection
    entry_time_ms: int
    entry_price: Decimal
    current_qty: Decimal
    trade_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrajectoryMetrics:
    symbol: str
    direction: TradeDirection
    entry_time_ms: int
    measured_until_ms: int
    entry_price: Decimal
    current_price: Decimal
    reference_qty: Decimal
    favorable_extreme_price: Decimal
    adverse_extreme_price: Decimal
    mfe_per_unit: Decimal
    mae_per_unit: Decimal
    mfe_pct: Decimal
    mae_pct: Decimal
    current_pnl_per_unit: Decimal
    observation_count: int
    holding_time_ms: int
    quality: TrajectoryQuality
    history_complete: bool
    initial_stop_loss: Decimal | None = None
    mfe_r: Decimal | None = None
    mae_r: Decimal | None = None


def _signed_qty(fill: UserTradeFill) -> Decimal:
    if fill.position_side != "BOTH":
        raise TrajectoryError("Hedge Mode fill history is not supported")
    if fill.side == "BUY":
        return fill.qty
    if fill.side == "SELL":
        return -fill.qty
    raise TrajectoryError(f"unexpected fill side: {fill.side}")


def infer_open_episode(
    position: PositionSnapshot,
    fills: tuple[UserTradeFill, ...],
) -> OpenEpisodeEvidence:
    """Infer the current flat-to-open episode from Binance fills and REST position state."""
    if not position.is_open:
        raise TrajectoryError("position must be open")
    if position.avg_price is None or position.avg_price <= 0:
        raise TrajectoryError("authoritative average entry price is missing")

    expected = position.size if position.side == "Buy" else -position.size
    symbol_fills = sorted(
        (fill for fill in fills if fill.symbol == position.symbol),
        key=lambda item: (item.time_ms, item.trade_id),
    )
    if not symbol_fills:
        raise TrajectoryError("no Binance fill history is available for open position")

    running = Decimal(0)
    episode_start_ms: int | None = None
    episode_trade_ids: list[str] = []
    seen_trade_ids: set[str] = set()

    for fill in symbol_fills:
        if fill.trade_id in seen_trade_ids:
            raise TrajectoryError("duplicate trade id in fill history")
        seen_trade_ids.add(fill.trade_id)
        if fill.qty <= 0 or fill.price <= 0:
            raise TrajectoryError("fill quantity and price must be positive")

        signed = _signed_qty(fill)
        before = running
        after = before + signed
        if before != 0 and after != 0 and (before > 0) != (after > 0):
            raise TrajectoryError("fill history reverses position without becoming flat")

        if before == 0 and after != 0:
            episode_start_ms = fill.time_ms
            episode_trade_ids = []
        if episode_start_ms is not None:
            episode_trade_ids.append(fill.trade_id)
        if after == 0:
            episode_start_ms = None
            episode_trade_ids = []
        running = after

    if running != expected:
        raise TrajectoryError(
            f"fill history ends at signed qty={running}, exchange position={expected}"
        )
    if episode_start_ms is None:
        raise TrajectoryError("current open episode start cannot be established")

    direction = TradeDirection.LONG if position.side == "Buy" else TradeDirection.SHORT
    return OpenEpisodeEvidence(
        symbol=position.symbol,
        direction=direction,
        entry_time_ms=episode_start_ms,
        entry_price=position.avg_price,
        current_qty=position.size,
        trade_ids=tuple(episode_trade_ids),
    )


def _first_full_minute(entry_time_ms: int) -> int:
    if entry_time_ms % _ONE_MINUTE_MS == 0:
        return entry_time_ms
    return ((entry_time_ms // _ONE_MINUTE_MS) + 1) * _ONE_MINUTE_MS


def _last_full_minute_start(measured_until_ms: int) -> int:
    return (measured_until_ms // _ONE_MINUTE_MS) * _ONE_MINUTE_MS - _ONE_MINUTE_MS


def _validate_candle(candle: Candle) -> None:
    if candle.low <= 0 or candle.high <= 0 or candle.close <= 0:
        raise TrajectoryError("candle prices must be positive")
    if candle.low > candle.high:
        raise TrajectoryError("candle low cannot exceed high")


def _history_is_complete(
    candles: tuple[Candle, ...],
    first_full_start_ms: int,
    measured_until_ms: int,
) -> bool:
    last_full_start_ms = _last_full_minute_start(measured_until_ms)
    if last_full_start_ms < first_full_start_ms:
        return True
    starts = tuple(
        candle.start_time_ms
        for candle in candles
        if first_full_start_ms <= candle.start_time_ms <= last_full_start_ms
    )
    if not starts or starts[0] != first_full_start_ms or starts[-1] != last_full_start_ms:
        return False
    return all(
        current - previous == _ONE_MINUTE_MS
        for previous, current in zip(starts, starts[1:], strict=False)
    )


def reconstruct_conservative_trajectory(
    episode: OpenEpisodeEvidence,
    candles: tuple[Candle, ...],
    *,
    measured_until_ms: int,
    current_price: Decimal,
    initial_stop_loss: Decimal | None = None,
) -> TrajectoryMetrics:
    """Replay only full 1m candles so partial-minute extremes are never fabricated."""
    if measured_until_ms < episode.entry_time_ms:
        raise TrajectoryError("measurement time precedes entry")
    if current_price <= 0:
        raise TrajectoryError("current price must be positive")
    if episode.current_qty <= 0 or episode.entry_price <= 0:
        raise TrajectoryError("entry price and reference quantity must be positive")

    ordered = tuple(sorted(candles, key=lambda item: item.start_time_ms))
    if len({item.start_time_ms for item in ordered}) != len(ordered):
        raise TrajectoryError("duplicate candle timestamps are not allowed")
    for candle in ordered:
        _validate_candle(candle)

    first_full_start_ms = _first_full_minute(episode.entry_time_ms)
    full = tuple(
        candle
        for candle in ordered
        if candle.start_time_ms >= first_full_start_ms
        and candle.start_time_ms + _ONE_MINUTE_MS <= measured_until_ms
    )
    history_complete = _history_is_complete(
        full,
        first_full_start_ms,
        measured_until_ms,
    )

    favorable = episode.entry_price
    adverse = episode.entry_price
    if episode.direction is TradeDirection.LONG:
        for candle in full:
            favorable = max(favorable, candle.high)
            adverse = min(adverse, candle.low)
        favorable = max(favorable, current_price)
        adverse = min(adverse, current_price)
        mfe_per_unit = favorable - episode.entry_price
        mae_per_unit = episode.entry_price - adverse
        current_pnl_per_unit = current_price - episode.entry_price
    else:
        for candle in full:
            favorable = min(favorable, candle.low)
            adverse = max(adverse, candle.high)
        favorable = min(favorable, current_price)
        adverse = max(adverse, current_price)
        mfe_per_unit = episode.entry_price - favorable
        mae_per_unit = adverse - episode.entry_price
        current_pnl_per_unit = episode.entry_price - current_price

    mfe_r: Decimal | None = None
    mae_r: Decimal | None = None
    if initial_stop_loss is not None:
        if episode.direction is TradeDirection.LONG:
            valid_stop = Decimal(0) < initial_stop_loss < episode.entry_price
        else:
            valid_stop = initial_stop_loss > episode.entry_price
        if not valid_stop:
            raise TrajectoryError("initial stop loss is invalid for trajectory direction")
        initial_risk_per_unit = abs(episode.entry_price - initial_stop_loss)
        mfe_r = mfe_per_unit / initial_risk_per_unit
        mae_r = mae_per_unit / initial_risk_per_unit

    quality = (
        TrajectoryQuality.CONSERVATIVE_1M_REPLAY
        if history_complete
        else TrajectoryQuality.PARTIAL_HISTORY
    )
    return TrajectoryMetrics(
        symbol=episode.symbol,
        direction=episode.direction,
        entry_time_ms=episode.entry_time_ms,
        measured_until_ms=measured_until_ms,
        entry_price=episode.entry_price,
        current_price=current_price,
        reference_qty=episode.current_qty,
        favorable_extreme_price=favorable,
        adverse_extreme_price=adverse,
        mfe_per_unit=mfe_per_unit,
        mae_per_unit=mae_per_unit,
        mfe_pct=mfe_per_unit / episode.entry_price * Decimal("100"),
        mae_pct=mae_per_unit / episode.entry_price * Decimal("100"),
        current_pnl_per_unit=current_pnl_per_unit,
        observation_count=len(full),
        holding_time_ms=measured_until_ms - episode.entry_time_ms,
        quality=quality,
        history_complete=history_complete,
        initial_stop_loss=initial_stop_loss,
        mfe_r=mfe_r,
        mae_r=mae_r,
    )
