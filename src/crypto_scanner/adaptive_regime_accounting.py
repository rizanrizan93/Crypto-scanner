from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass

from crypto_scanner.adaptive_h4_data import FundingPoint
from crypto_scanner.adaptive_regime_backtest import RawTrade


@dataclass(frozen=True, slots=True)
class TradeResult:
    symbol: str
    entry_ms: int
    exit_ms: int
    direction: int
    base_r: float
    stress_r: float
    severe_r: float
    exit_reason: str


def apply_costs(raw: tuple[RawTrade, ...], funding: tuple[FundingPoint, ...]) -> tuple[TradeResult, ...]:
    times = [point.funding_time_ms for point in funding]
    out: list[TradeResult] = []
    for trade in raw:
        start = bisect_left(times, trade.entry_ms + 1)
        end = bisect_left(times, trade.exit_ms)
        paid = sum(funding[i].funding_rate for i in range(start, end))
        funding_return = -trade.direction * paid
        stressed = funding_return * (0.8 if funding_return >= 0 else 1.2)
        base = trade.gross_r + (funding_return - 0.0008) / trade.risk_return
        stress = trade.gross_r + (stressed - 0.0014) / trade.risk_return
        severe = trade.gross_r + (stressed - 0.0025) / trade.risk_return
        out.append(TradeResult(trade.symbol, trade.entry_ms, trade.exit_ms, trade.direction, base, stress, severe, trade.exit_reason))
    return tuple(out)
