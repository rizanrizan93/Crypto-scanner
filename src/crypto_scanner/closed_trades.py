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

    @property
    def holding_time_ms(self) -> int:
        return self.exit_time_ms - self.entry_time_ms


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


def reconstruct_closed_trades(
    fills: tuple[UserTradeFill, ...],
    income: tuple[IncomeRecord, ...] = (),
) -> tuple[ClosedTradeEvidence, ...]:
    """Reconstruct flat-to-flat episodes under the one-position-per-symbol contract."""
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

        for fill in sorted(symbol_fills, key=lambda item: (item.time_ms, item.trade_id)):
            signed = _signed_qty(fill)
            before = position
            after = before + signed
            if before != 0 and after != 0 and (before > 0) != (after > 0):
                raise ClosedTradeError(
                    f"fill history reverses {symbol} without becoming flat; fail closed"
                )

            if before == 0:
                direction = TradeDirection.LONG if signed > 0 else TradeDirection.SHORT
                entry_time_ms = fill.time_ms
                opening_qty = Decimal(0)
                opening_notional = Decimal(0)
                closing_qty = Decimal(0)
                closing_notional = Decimal(0)
                realized_pnl = Decimal(0)
                commission = Decimal(0)
                trade_ids = []

            assert direction is not None
            increasing = before == 0 or (before > 0 and signed > 0) or (before < 0 and signed < 0)
            if increasing:
                opening_qty += fill.qty
                opening_notional += fill.qty * fill.price
            else:
                if fill.qty > abs(before):
                    raise ClosedTradeError(
                        f"closing fill exceeds open {symbol} position; fail closed"
                    )
                closing_qty += fill.qty
                closing_notional += fill.qty * fill.price

            realized_pnl += fill.realized_pnl
            commission += fill.commission
            trade_ids.append(fill.trade_id)
            position = after

            if position == 0:
                if opening_qty <= 0 or closing_qty <= 0 or opening_qty != closing_qty:
                    raise ClosedTradeError(
                        f"flat {symbol} episode has inconsistent opening/closing quantity"
                    )
                funding_fee = _funding_for_window(
                    income,
                    symbol,
                    entry_time_ms,
                    fill.time_ms,
                )
                results.append(
                    ClosedTradeEvidence(
                        symbol=symbol,
                        direction=direction,
                        entry_time_ms=entry_time_ms,
                        exit_time_ms=fill.time_ms,
                        entry_qty=opening_qty,
                        exit_qty=closing_qty,
                        average_entry_price=opening_notional / opening_qty,
                        average_exit_price=closing_notional / closing_qty,
                        realized_pnl=realized_pnl,
                        commission=commission,
                        funding_fee=funding_fee,
                        net_pnl=realized_pnl + funding_fee - commission,
                        trade_ids=tuple(trade_ids),
                    )
                )
                direction = None

    return tuple(sorted(results, key=lambda item: (item.exit_time_ms, item.symbol)))
