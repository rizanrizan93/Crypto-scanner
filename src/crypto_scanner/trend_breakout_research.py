from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from crypto_scanner.binance.models import Candle

Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True, slots=True)
class TrendBreakoutCandidate:
    name: str
    lookback: int
    atr_stop: Decimal
    target_r: Decimal
    compression_ratio_max: Decimal | None = None
    expansion_atr_min: Decimal = Decimal("0")


# Frozen registry. Do not alter after validation/OOS results are inspected.
FROZEN_CANDIDATES = (
    TrendBreakoutCandidate(
        name="CRYPTO_DONCHIAN20_2R",
        lookback=20,
        atr_stop=Decimal("2.0"),
        target_r=Decimal("2.0"),
    ),
    TrendBreakoutCandidate(
        name="CRYPTO_DONCHIAN20_3R",
        lookback=20,
        atr_stop=Decimal("2.0"),
        target_r=Decimal("3.0"),
    ),
    TrendBreakoutCandidate(
        name="CRYPTO_DONCHIAN55_2R",
        lookback=55,
        atr_stop=Decimal("2.0"),
        target_r=Decimal("2.0"),
    ),
    TrendBreakoutCandidate(
        name="CRYPTO_COMP20_2R",
        lookback=20,
        atr_stop=Decimal("1.5"),
        target_r=Decimal("2.0"),
        compression_ratio_max=Decimal("5.0"),
        expansion_atr_min=Decimal("1.0"),
    ),
)


@dataclass(frozen=True, slots=True)
class TrendBreakoutTrade:
    symbol: str
    candidate: str
    direction: Direction
    decision_time_ms: int
    entry_time_ms: int
    exit_time_ms: int
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    gross_r: Decimal
    net_r: Decimal
    exit_reason: str


@dataclass(frozen=True, slots=True)
class TrendBreakoutSummary:
    trades: int
    wins: int
    losses: int
    win_rate: Decimal
    expectancy_r: Decimal
    profit_factor: Decimal | None
    total_net_r: Decimal
    max_drawdown_r: Decimal


def _ema(values: list[Decimal], period: int) -> list[Decimal | None]:
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return result
    p = Decimal(period)
    seed = sum(values[:period], Decimal(0)) / p
    result[period - 1] = seed
    alpha = Decimal(2) / Decimal(period + 1)
    current = seed
    for index in range(period, len(values)):
        current = alpha * values[index] + (Decimal(1) - alpha) * current
        result[index] = current
    return result


def _atr(rows: tuple[Candle, ...], period: int = 14) -> list[Decimal | None]:
    trs: list[Decimal] = []
    for index, row in enumerate(rows):
        if index == 0:
            tr = row.high - row.low
        else:
            previous = rows[index - 1].close
            tr = max(row.high - row.low, abs(row.high - previous), abs(row.low - previous))
        trs.append(max(Decimal(0), tr))
    result: list[Decimal | None] = [None] * len(rows)
    if len(trs) < period:
        return result
    current = sum(trs[:period], Decimal(0)) / Decimal(period)
    result[period - 1] = current
    for index in range(period, len(trs)):
        current = (Decimal(period - 1) * current + trs[index]) / Decimal(period)
        result[index] = current
    return result


def _validate(rows: Iterable[Candle]) -> tuple[Candle, ...]:
    result = tuple(sorted(rows, key=lambda row: row.start_time_ms))
    if any(
        second.start_time_ms <= first.start_time_ms
        for first, second in zip(result, result[1:], strict=False)
    ):
        raise ValueError("candles must be strictly increasing")
    return result


def replay_candidate(
    candles: Iterable[Candle],
    *,
    symbol: str,
    candidate: TrendBreakoutCandidate,
    max_hold_bars: int = 72,
) -> tuple[TrendBreakoutTrade, ...]:
    rows = _validate(candles)
    if not rows:
        return ()
    closes = [row.close for row in rows]
    ema200 = _ema(closes, 200)
    atr14 = _atr(rows, 14)
    warmup = max(200, candidate.lookback + 1)
    next_allowed = warmup
    output: list[TrendBreakoutTrade] = []

    for index in range(warmup, len(rows) - 1):
        if index < next_allowed:
            continue
        atr = atr14[index - 1]
        ema = ema200[index - 1]
        if atr is None or ema is None or atr <= 0:
            continue
        prior = rows[index - candidate.lookback:index]
        high = max(row.high for row in prior)
        low = min(row.low for row in prior)
        decision = rows[index]
        direction: Direction | None = None
        if decision.close > high and decision.close > ema:
            direction = "LONG"
        elif decision.close < low and decision.close < ema:
            direction = "SHORT"
        if direction is None:
            continue

        if candidate.compression_ratio_max is not None:
            if (high - low) / atr > candidate.compression_ratio_max:
                continue
            previous = rows[index - 1].close
            true_range = max(
                decision.high - decision.low,
                abs(decision.high - previous),
                abs(decision.low - previous),
            )
            if true_range / atr < candidate.expansion_atr_min:
                continue

        entry_index = index + 1
        entry = rows[entry_index]
        entry_price = entry.open
        risk = candidate.atr_stop * atr
        if risk <= 0:
            continue
        if direction == "LONG":
            stop = entry_price - risk
            target = entry_price + candidate.target_r * risk
        else:
            stop = entry_price + risk
            target = entry_price - candidate.target_r * risk

        planned_exit = min(len(rows) - 1, entry_index + max_hold_bars - 1)
        actual_exit = planned_exit
        exit_reason = "TIME_EXIT"
        exit_price = rows[planned_exit].close
        gross_r = (
            (exit_price - entry_price) / risk
            if direction == "LONG"
            else (entry_price - exit_price) / risk
        )
        for probe_index in range(entry_index, planned_exit + 1):
            probe = rows[probe_index]
            if direction == "LONG":
                stop_hit = probe.low <= stop
                target_hit = probe.high >= target
            else:
                stop_hit = probe.high >= stop
                target_hit = probe.low <= target
            # Conservative intrabar ambiguity handling.
            if stop_hit:
                gross_r = Decimal("-1")
                exit_price = stop
                exit_reason = "STOP"
                actual_exit = probe_index
                break
            if target_hit:
                gross_r = candidate.target_r
                exit_price = target
                exit_reason = "TARGET"
                actual_exit = probe_index
                break

        output.append(
            TrendBreakoutTrade(
                symbol=symbol,
                candidate=candidate.name,
                direction=direction,
                decision_time_ms=decision.start_time_ms,
                entry_time_ms=entry.start_time_ms,
                exit_time_ms=rows[actual_exit].start_time_ms,
                entry_price=entry_price,
                stop_loss=stop,
                take_profit=target,
                gross_r=gross_r,
                net_r=gross_r,
                exit_reason=exit_reason,
            )
        )
        next_allowed = actual_exit + 1
    return tuple(output)


def reprice_cost(
    trades: Iterable[TrendBreakoutTrade], *, round_trip_cost_bps: Decimal
) -> tuple[TrendBreakoutTrade, ...]:
    if round_trip_cost_bps < 0:
        raise ValueError("cost must be non-negative")
    output: list[TrendBreakoutTrade] = []
    for trade in trades:
        risk = abs(trade.entry_price - trade.stop_loss)
        if risk <= 0:
            raise ValueError("non-positive initial risk")
        cost_r = trade.entry_price * round_trip_cost_bps / Decimal("10000") / risk
        output.append(replace(trade, net_r=trade.gross_r - cost_r))
    return tuple(output)


def summarize(trades: Iterable[TrendBreakoutTrade]) -> TrendBreakoutSummary:
    rows = tuple(sorted(trades, key=lambda row: row.entry_time_ms))
    if not rows:
        return TrendBreakoutSummary(
            0, 0, 0, Decimal(0), Decimal(0), None, Decimal(0), Decimal(0)
        )
    wins = [row.net_r for row in rows if row.net_r > 0]
    losses = [row.net_r for row in rows if row.net_r < 0]
    equity = peak = drawdown = Decimal(0)
    for row in rows:
        equity += row.net_r
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    gross_profit = sum(wins, Decimal(0))
    gross_loss = -sum(losses, Decimal(0))
    return TrendBreakoutSummary(
        trades=len(rows),
        wins=len(wins),
        losses=len(losses),
        win_rate=Decimal(len(wins)) / Decimal(len(rows)),
        expectancy_r=sum((row.net_r for row in rows), Decimal(0)) / Decimal(len(rows)),
        profit_factor=(gross_profit / gross_loss if gross_loss > 0 else None),
        total_net_r=sum((row.net_r for row in rows), Decimal(0)),
        max_drawdown_r=drawdown,
    )


def trade_month(trade: TrendBreakoutTrade) -> tuple[int, int]:
    dt = datetime.fromtimestamp(trade.entry_time_ms / 1000, tz=UTC)
    return dt.year, dt.month


def split_calendar(
    trades: Iterable[TrendBreakoutTrade],
) -> dict[str, tuple[TrendBreakoutTrade, ...]]:
    result = {"train": [], "validation": [], "oos": []}
    for trade in sorted(trades, key=lambda row: row.entry_time_ms):
        year, month = trade_month(trade)
        if year == 2024:
            result["train"].append(trade)
        elif year == 2025:
            result["validation"].append(trade)
        elif year == 2026 and month <= 8:
            result["oos"].append(trade)
    return {key: tuple(value) for key, value in result.items()}


def summary_payload(trades: Iterable[TrendBreakoutTrade]) -> dict[str, object]:
    raw = asdict(summarize(trades))
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in raw.items()
    }
