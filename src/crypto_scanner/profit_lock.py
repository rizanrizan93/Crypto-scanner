from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient
from crypto_scanner.binance.private_write import BinanceTestnetOrderClient
from crypto_scanner.binance.public_rest import BinanceDemoPublicRestClient
from crypto_scanner.closed_trades import TradeDirection
from crypto_scanner.persistence import TransientPersistenceError
from crypto_scanner.position_manager import ProtectionStatus, audit_symbol_protection
from crypto_scanner.position_manager_write import PositionManagerError, replace_aggregate_protection
from crypto_scanner.strategy_params import StrategyParameters
from crypto_scanner.trade_linkage import DurableTradeLinkage
from crypto_scanner.trajectory import (
    TrajectoryError,
    infer_open_episode,
    reconstruct_conservative_trajectory,
)

PROFIT_LOCK_STEP_R = Decimal("0.50")
PROFIT_LOCK_MIN_R = Decimal("0.05")
PROFIT_LOCK_MARK_BUFFER_R = Decimal("0.10")


class ProfitLockError(RuntimeError):
    """Raised when an eligible open position cannot be ratcheted safely."""


class ProfitLockStatus(StrEnum):
    NO_ACTION_BELOW_TRIGGER = "NO_ACTION_BELOW_TRIGGER"
    NO_ACTION_ALREADY_TIGHTER = "NO_ACTION_ALREADY_TIGHTER"
    NO_ACTION_RETRACE_TOO_DEEP = "NO_ACTION_RETRACE_TOO_DEEP"
    SKIPPED_INELIGIBLE = "SKIPPED_INELIGIBLE"
    SKIPPED_LAYERED = "SKIPPED_LAYERED"
    SKIPPED_PARTIAL_HISTORY = "SKIPPED_PARTIAL_HISTORY"
    SKIPPED_UNPROTECTED = "SKIPPED_UNPROTECTED"
    DEGRADED_PERSISTENCE_TRANSIENT = "DEGRADED_PERSISTENCE_TRANSIENT"
    RATCHETED = "RATCHETED"


@dataclass(frozen=True, slots=True)
class ProfitLockDecision:
    symbol: str
    status: ProfitLockStatus
    signal_id: str | None
    mfe_r: Decimal | None
    current_r: Decimal | None
    locked_r: Decimal | None
    previous_stop: Decimal | None
    desired_stop: Decimal | None
    tp2_trigger: Decimal | None
    detail: str


def _floor_r(value: Decimal) -> Decimal:
    if value <= 0:
        return Decimal(0)
    steps = (value / PROFIT_LOCK_STEP_R).to_integral_value(rounding=ROUND_FLOOR)
    return steps * PROFIT_LOCK_STEP_R


def locked_r_from_mfe(mfe_r: Decimal, strategy: StrategyParameters) -> Decimal | None:
    """Return the staged R floor justified by observed favorable excursion.

    The activation and trailing gap are calibration-aware, while the 0.50R staircase
    stays fixed to avoid overfitting the exit path on a small sample.
    """
    strategy.validate()
    if mfe_r < strategy.profit_lock_activation_r:
        return None
    raw = max(Decimal(0), mfe_r - strategy.profit_lock_gap_r)
    staged = _floor_r(raw)
    return max(PROFIT_LOCK_MIN_R, staged)


def _round_stop(value: Decimal, tick_size: Decimal, direction: TradeDirection) -> Decimal:
    if tick_size <= 0:
        raise ProfitLockError("instrument tick size must be positive")
    rounding = ROUND_FLOOR if direction is TradeDirection.LONG else ROUND_CEILING
    steps = (value / tick_size).to_integral_value(rounding=rounding)
    rounded = steps * tick_size
    if rounded <= 0:
        raise ProfitLockError("rounded profit-lock stop is invalid")
    return rounded


def _stop_from_locked_r(
    *,
    entry_price: Decimal,
    initial_stop: Decimal,
    direction: TradeDirection,
    locked_r: Decimal,
    tick_size: Decimal,
) -> Decimal:
    risk = abs(entry_price - initial_stop)
    if risk <= 0:
        raise ProfitLockError("initial risk per unit must be positive")
    raw = (
        entry_price + locked_r * risk
        if direction is TradeDirection.LONG
        else entry_price - locked_r * risk
    )
    return _round_stop(raw, tick_size, direction)


def _is_tighter(
    *,
    direction: TradeDirection,
    candidate: Decimal,
    existing: Decimal,
) -> bool:
    if direction is TradeDirection.LONG:
        return candidate > existing
    return candidate < existing


def _safe_locked_r(
    *,
    target_locked_r: Decimal,
    current_r: Decimal,
) -> Decimal | None:
    """Clamp a late ratchet to the highest safe staged floor behind current mark.

    This prevents an immediately-triggering replacement when MFE occurred between runtime
    polls and price has already retraced. It never invents a higher lock than the MFE policy.
    """
    available = current_r - PROFIT_LOCK_MARK_BUFFER_R
    if available < PROFIT_LOCK_MIN_R:
        return None
    safe = _floor_r(available)
    if safe <= 0:
        safe = PROFIT_LOCK_MIN_R
    return min(target_locked_r, safe)


def run_profit_lock(
    reader: BinanceDemoPrivateReadOnlyClient,
    public: BinanceDemoPublicRestClient,
    writer: BinanceTestnetOrderClient,
    linkage: DurableTradeLinkage,
    strategy: StrategyParameters | None = None,
    *,
    now_ms: int | None = None,
) -> tuple[ProfitLockDecision, ...]:
    """Ratchet full-size scanner STOP_MARKET protectors using durable initial-R evidence.

    Replacement delegates to the existing crash-safe aggregate-protection protocol: the
    new full-size stop and TP2 are submitted and authoritatively reconciled before stale
    scanner-owned protectors are cancelled. TP2 trigger is preserved exactly.
    """
    measured_until_ms = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    positions = tuple(position for position in reader.get_positions() if position.is_open)
    decisions: list[ProfitLockDecision] = []

    for position in positions:
        symbol = position.symbol
        algos = reader.get_open_algo_orders(symbol)
        report = audit_symbol_protection(symbol, positions, algos)
        if (
            report.status is not ProtectionStatus.PROTECTED
            or report.stop is None
            or report.take_profit is None
            or report.stop.trigger_price is None
            or report.take_profit.trigger_price is None
        ):
            decisions.append(
                ProfitLockDecision(
                    symbol=symbol,
                    status=ProfitLockStatus.SKIPPED_UNPROTECTED,
                    signal_id=None,
                    mfe_r=None,
                    current_r=None,
                    locked_r=None,
                    previous_stop=(report.stop.trigger_price if report.stop else None),
                    desired_stop=None,
                    tp2_trigger=(report.take_profit.trigger_price if report.take_profit else None),
                    detail=f"protection_status={report.status.value}",
                )
            )
            continue

        try:
            fills = reader.get_user_trades(symbol, limit=1000)
            episode = infer_open_episode(position, fills)
            if episode.layered_entry:
                decisions.append(
                    ProfitLockDecision(
                        symbol=symbol,
                        status=ProfitLockStatus.SKIPPED_LAYERED,
                        signal_id=None,
                        mfe_r=None,
                        current_r=None,
                        locked_r=None,
                        previous_stop=report.stop.trigger_price,
                        desired_stop=None,
                        tp2_trigger=report.take_profit.trigger_price,
                        detail="layered net position lacks reliable single-R exit attribution",
                    )
                )
                continue

            context = linkage.resolve_context(
                symbol=symbol,
                direction=episode.direction,
                entry_time_ms=episode.entry_time_ms,
            )
            if (
                context.strategy_params is None
                or not context.strategy_id
                or not context.calibration_eligible
                or context.signal_id is None
                or context.initial_stop_loss is None
            ):
                decisions.append(
                    ProfitLockDecision(
                        symbol=symbol,
                        status=ProfitLockStatus.SKIPPED_INELIGIBLE,
                        signal_id=context.signal_id,
                        mfe_r=None,
                        current_r=None,
                        locked_r=None,
                        previous_stop=report.stop.trigger_price,
                        desired_stop=None,
                        tp2_trigger=report.take_profit.trigger_price,
                        detail="durable scanner signal/initial-risk chain is incomplete",
                    )
                )
                continue

            mark_price = position.mark_price
            if mark_price is None or mark_price <= 0:
                mark_price = public.get_ticker(symbol).mark_price
            candles = public.get_klines_window(
                symbol,
                "1",
                start_time_ms=episode.entry_time_ms,
                end_time_ms=measured_until_ms + 1,
            )
            metrics = reconstruct_conservative_trajectory(
                episode,
                candles,
                measured_until_ms=measured_until_ms,
                current_price=mark_price,
                initial_stop_loss=context.initial_stop_loss,
            )
            if not metrics.history_complete:
                decisions.append(
                    ProfitLockDecision(
                        symbol=symbol,
                        status=ProfitLockStatus.SKIPPED_PARTIAL_HISTORY,
                        signal_id=context.signal_id,
                        mfe_r=metrics.mfe_r,
                        current_r=None,
                        locked_r=None,
                        previous_stop=report.stop.trigger_price,
                        desired_stop=None,
                        tp2_trigger=report.take_profit.trigger_price,
                        detail="1m trajectory history is incomplete; fail closed",
                    )
                )
                continue
            if metrics.mfe_r is None:
                raise ProfitLockError("eligible trajectory unexpectedly lacks mfe_r")

            initial_risk = abs(episode.entry_price - context.initial_stop_loss)
            if initial_risk <= 0:
                raise ProfitLockError("eligible trajectory has invalid initial risk")
            current_r = metrics.current_pnl_per_unit / initial_risk
            target_locked_r = locked_r_from_mfe(metrics.mfe_r, context.strategy_params)
            if target_locked_r is None:
                decisions.append(
                    ProfitLockDecision(
                        symbol=symbol,
                        status=ProfitLockStatus.NO_ACTION_BELOW_TRIGGER,
                        signal_id=context.signal_id,
                        mfe_r=metrics.mfe_r,
                        current_r=current_r,
                        locked_r=None,
                        previous_stop=report.stop.trigger_price,
                        desired_stop=None,
                        tp2_trigger=report.take_profit.trigger_price,
                        detail="MFE has not reached calibrated profit-lock activation",
                    )
                )
                continue

            effective_locked_r = _safe_locked_r(
                target_locked_r=target_locked_r,
                current_r=current_r,
            )
            if effective_locked_r is None:
                decisions.append(
                    ProfitLockDecision(
                        symbol=symbol,
                        status=ProfitLockStatus.NO_ACTION_RETRACE_TOO_DEEP,
                        signal_id=context.signal_id,
                        mfe_r=metrics.mfe_r,
                        current_r=current_r,
                        locked_r=None,
                        previous_stop=report.stop.trigger_price,
                        desired_stop=None,
                        tp2_trigger=report.take_profit.trigger_price,
                        detail="price already retraced inside the safe stop-replacement buffer",
                    )
                )
                continue

            instrument = public.get_instrument(symbol)
            desired_stop = _stop_from_locked_r(
                entry_price=episode.entry_price,
                initial_stop=context.initial_stop_loss,
                direction=episode.direction,
                locked_r=effective_locked_r,
                tick_size=instrument.tick_size,
            )
            previous_stop = report.stop.trigger_price
            if not _is_tighter(
                direction=episode.direction,
                candidate=desired_stop,
                existing=previous_stop,
            ):
                decisions.append(
                    ProfitLockDecision(
                        symbol=symbol,
                        status=ProfitLockStatus.NO_ACTION_ALREADY_TIGHTER,
                        signal_id=context.signal_id,
                        mfe_r=metrics.mfe_r,
                        current_r=current_r,
                        locked_r=effective_locked_r,
                        previous_stop=previous_stop,
                        desired_stop=desired_stop,
                        tp2_trigger=report.take_profit.trigger_price,
                        detail="existing scanner stop is already at least as protective",
                    )
                )
                continue

            seed = (
                f"profit-lock:{context.position_id}:"
                f"{effective_locked_r}:{desired_stop}:{report.take_profit.trigger_price}"
            )
            replace_aggregate_protection(
                reader,
                writer,
                symbol=symbol,
                stop_trigger=desired_stop,
                tp2_trigger=report.take_profit.trigger_price,
                management_seed=seed,
            )
            decisions.append(
                ProfitLockDecision(
                    symbol=symbol,
                    status=ProfitLockStatus.RATCHETED,
                    signal_id=context.signal_id,
                    mfe_r=metrics.mfe_r,
                    current_r=current_r,
                    locked_r=effective_locked_r,
                    previous_stop=previous_stop,
                    desired_stop=desired_stop,
                    tp2_trigger=report.take_profit.trigger_price,
                    detail="full-size stop ratcheted; TP2 trigger preserved",
                )
            )
        except TransientPersistenceError as exc:
            decisions.append(
                ProfitLockDecision(
                    symbol=symbol,
                    status=ProfitLockStatus.DEGRADED_PERSISTENCE_TRANSIENT,
                    signal_id=None,
                    mfe_r=None,
                    current_r=None,
                    locked_r=None,
                    previous_stop=report.stop.trigger_price,
                    desired_stop=None,
                    tp2_trigger=report.take_profit.trigger_price,
                    detail=f"{exc}; exchange protectors preserved; no new risk",
                )
            )
            # Stop querying this dependency for the rest of this tick.
            break
        except (TrajectoryError, PositionManagerError, ValueError, RuntimeError) as exc:
            raise ProfitLockError(f"profit lock failed for {symbol}: {exc}") from exc

    return tuple(decisions)
