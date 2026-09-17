from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from crypto_scanner.binance.private_rest import IncomeRecord, UserTradeFill


class ClosedTradeError(RuntimeError):
    """Raised when fill history cannot be reconstructed without ambiguity."""


class TradeDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True, slots=True)
class ClosedTradeEvidence:
    symbol: str
    direction: TradeDirection
    entry_time_ms: int
    exit_time_ms: int
    entry_qty: Decimal
    exit_qty: Decimal
    average_entry_price: Decimal
    average_exit_price: Decimal
    realized_pnl: Decimal
    commission: Decimal
    funding_fee: Decimal
    net_pnl: Decimal
    trade_ids: tuple[str, ...]
    entry_order_ids: tuple[str, ...] = ()

    @property
    def holding_time_ms(self) -> int:
        return self.exit_time_ms - self.entry_time_ms

    @property
    def layered_entry(self) -> bool:
        """True only when the episode increased exposure through multiple entry orders."""
        return len(self.entry_order_ids) > 1


def _signed_qty(fill: UserTradeFill) -> Decimal:
    if fill.position_side != "BOTH":
        raise ClosedTradeError("Hedge Mode fill history is not supported")
    if fill.side == "BUY":
        return fill.qty
    if fill.side == "SELL":
        return -fill.qty
    raise ClosedTradeError(f"unexpected fill side: {fill.side}")


def _funding_for_window(
    income: tuple[IncomeRecord, ...],
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> Decimal:
    return sum(
        (
            record.income
            for record in income
            if record.symbol == symbol
            and record.income_type == "FUNDING_FEE"
            and start_ms <= record.time_ms <= end_ms
        ),
        Decimal(0),
    )


def _closed_episode(
    *,
    income: tuple[IncomeRecord, ...],
    symbol: str,
    direction: TradeDirection | None,
    entry_time_ms: int,
    exit_time_ms: int,
    opening_qty: Decimal,
    opening_notional: Decimal,
    closing_qty: Decimal,
    closing_notional: Decimal,
    realized_pnl: Decimal,
    commission: Decimal,
    trade_ids: list[str],
    entry_order_ids: list[str],
) -> ClosedTradeEvidence:
    if direction is None:
        raise ClosedTradeError(f"flat {symbol} episode has no direction")
    if opening_qty <= 0 or closing_qty <= 0 or opening_qty != closing_qty:
        raise ClosedTradeError(
            f"flat {symbol} episode has inconsistent opening/closing quantity"
        )
    funding_fee = _funding_for_window(income, symbol, entry_time_ms, exit_time_ms)
    return ClosedTradeEvidence(
        symbol=symbol,
        direction=direction,
        entry_time_ms=entry_time_ms,
        exit_time_ms=exit_time_ms,
        entry_qty=opening_qty,
        exit_qty=closing_qty,
        average_entry_price=opening_notional / opening_qty,
        average_exit_price=closing_notional / closing_qty,
        realized_pnl=realized_pnl,
        commission=commission,
        funding_fee=funding_fee,
        net_pnl=realized_pnl + funding_fee - commission,
        trade_ids=tuple(trade_ids),
        entry_order_ids=tuple(entry_order_ids),
    )


def reconstruct_closed_trades(
    fills: tuple[UserTradeFill, ...],
    income: tuple[IncomeRecord, ...] = (),
) -> tuple[ClosedTradeEvidence, ...]:
    """Reconstruct One-way net-position episodes, including atomic reversals.

    Binance One-way mode can execute one opposite-side fill whose quantity is
    larger than the current position. Economically that single fill first closes
    the old episode and then opens the residual quantity in the opposite
    direction at the same execution price/time. Treating that as corruption
    starves trajectory/calibration evidence, so the fill is deterministically
    split into a closing segment and a residual opening segment.

    Binance reports realized PnL only for the closing component of a reversal
    fill. Commission applies to the full fill and is allocated pro-rata by
    quantity between the closing and residual-opening segments.
    """
    results: list[ClosedTradeEvidence] = []
    by_symbol: dict[str, list[UserTradeFill]] = {}
    for fill in fills:
        if fill.qty <= 0 or fill.price <= 0:
            raise ClosedTradeError("fill quantity and price must be positive")
        by_symbol.setdefault(fill.symbol, []).append(fill)

    for symbol, symbol_fills in by_symbol.items():
        position = Decimal(0)
        direction: TradeDirection | None = None
        entry_time_ms = 0
        opening_qty = Decimal(0)
        opening_notional = Decimal(0)
        closing_qty = Decimal(0)
        closing_notional = Decimal(0)
        realized_pnl = Decimal(0)
        commission = Decimal(0)
        trade_ids: list[str] = []
        entry_order_ids: list[str] = []
        seen_entry_order_ids: set[str] = set()

        for fill in sorted(symbol_fills, key=lambda item: (item.time_ms, item.trade_id)):
            signed = _signed_qty(fill)
            before = position
            after = before + signed

            if before == 0:
                direction = TradeDirection.LONG if signed > 0 else TradeDirection.SHORT
                entry_time_ms = fill.time_ms
                opening_qty = fill.qty
                opening_notional = fill.qty * fill.price
                closing_qty = Decimal(0)
                closing_notional = Decimal(0)
                realized_pnl = fill.realized_pnl
                commission = fill.commission
                trade_ids = [fill.trade_id]
                entry_order_ids = [fill.order_id]
                seen_entry_order_ids = {fill.order_id}
                position = after
                continue

            assert direction is not None
            increasing = (before > 0 and signed > 0) or (before < 0 and signed < 0)
            if increasing:
                opening_qty += fill.qty
                opening_notional += fill.qty * fill.price
                realized_pnl += fill.realized_pnl
                commission += fill.commission
                trade_ids.append(fill.trade_id)
                if fill.order_id not in seen_entry_order_ids:
                    seen_entry_order_ids.add(fill.order_id)
                    entry_order_ids.append(fill.order_id)
                position = after
                continue

            close_qty = min(fill.qty, abs(before))
            residual_qty = fill.qty - close_qty
            if close_qty <= 0:
                raise ClosedTradeError(f"invalid closing quantity for {symbol}")

            close_commission = fill.commission * close_qty / fill.qty
            residual_commission = fill.commission - close_commission
            closing_qty += close_qty
            closing_notional += close_qty * fill.price
            realized_pnl += fill.realized_pnl
            commission += close_commission
            trade_ids.append(fill.trade_id)

            if residual_qty == 0:
                position = after
                if position == 0:
                    results.append(
                        _closed_episode(
                            income=income,
                            symbol=symbol,
                            direction=direction,
                            entry_time_ms=entry_time_ms,
                            exit_time_ms=fill.time_ms,
                            opening_qty=opening_qty,
                            opening_notional=opening_notional,
                            closing_qty=closing_qty,
                            closing_notional=closing_notional,
                            realized_pnl=realized_pnl,
                            commission=commission,
                            trade_ids=trade_ids,
                            entry_order_ids=entry_order_ids,
                        )
                    )
                    direction = None
                continue

            if close_qty != abs(before) or residual_qty != abs(after):
                raise ClosedTradeError(
                    f"reversal quantity for {symbol} is internally inconsistent"
                )
            results.append(
                _closed_episode(
                    income=income,
                    symbol=symbol,
                    direction=direction,
                    entry_time_ms=entry_time_ms,
                    exit_time_ms=fill.time_ms,
                    opening_qty=opening_qty,
                    opening_notional=opening_notional,
                    closing_qty=closing_qty,
                    closing_notional=closing_notional,
                    realized_pnl=realized_pnl,
                    commission=commission,
                    trade_ids=trade_ids,
                    entry_order_ids=entry_order_ids,
                )
            )

            direction = TradeDirection.LONG if signed > 0 else TradeDirection.SHORT
            entry_time_ms = fill.time_ms
            opening_qty = residual_qty
            opening_notional = residual_qty * fill.price
            closing_qty = Decimal(0)
            closing_notional = Decimal(0)
            realized_pnl = Decimal(0)
            commission = residual_commission
            trade_ids = [fill.trade_id]
            entry_order_ids = [fill.order_id]
            seen_entry_order_ids = {fill.order_id}
            position = after

    return tuple(sorted(results, key=lambda item: (item.exit_time_ms, item.symbol)))
