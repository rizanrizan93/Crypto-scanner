from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from crypto_scanner.binance.models import Candle, FundingRatePoint
from crypto_scanner.discovery import DiscoveryResult, TradeDirection
from crypto_scanner.regime_specialist_demo import DAY_MS

FUNDING_OI_STRATEGY_ID = "strategy-funding-z2-oi2-v1"
FUNDING_Z_THRESHOLD = 2.0
FUNDING_LOOKBACK_DAYS = 60
MIN_FUNDING_DAYS = 40
MOMENTUM_DAYS = 3
OI_GROWTH_THRESHOLD = Decimal("0.02")
INVOL_LOOKBACK = 20
MAX_CANDIDATES = 4
# Conservative stop-risk budget for the complete Funding+OI sleeve.
SLEEVE_STOP_RISK_BUDGET = Decimal("0.0025")


class FundingOiMarketSource(Protocol):
    def get_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int = 200,
    ) -> tuple[Candle, ...]: ...

    def get_funding_history(
        self,
        symbol: str,
        *,
        limit: int = 50,
    ) -> tuple[FundingRatePoint, ...]: ...


@dataclass(frozen=True, slots=True)
class FundingOiLeg:
    symbol: str
    direction: TradeDirection
    funding_z: Decimal
    momentum_3d: Decimal
    oi_growth: Decimal
    target_weight: Decimal
    risk_fraction: Decimal


@dataclass(frozen=True, slots=True)
class FundingOiDecision:
    as_of_ms: int
    signal_day: str | None
    legs: tuple[FundingOiLeg, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    symbol: str
    direction: TradeDirection
    funding_z: float
    momentum_3d: Decimal
    daily_vol: float


def _closed_daily(candles: tuple[Candle, ...], *, now_ms: int) -> tuple[Candle, ...]:
    return tuple(
        candle
        for candle in sorted(candles, key=lambda item: item.start_time_ms)
        if candle.start_time_ms + DAY_MS <= now_ms
    )


def _utc_day(timestamp_ms: int) -> date:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).date()


def _daily_funding(points: tuple[FundingRatePoint, ...]) -> dict[date, Decimal]:
    result: dict[date, Decimal] = {}
    for point in points:
        day = _utc_day(point.timestamp_ms)
        result[day] = result.get(day, Decimal(0)) + point.funding_rate
    return result


def _daily_vol(candles: tuple[Candle, ...], lookback: int) -> float | None:
    if len(candles) < lookback + 1:
        return None
    rows = candles[-(lookback + 1) :]
    values = [
        float(current.close / previous.close - Decimal(1))
        for previous, current in zip(rows, rows[1:], strict=False)
        if previous.close > 0
    ]
    if len(values) < max(10, lookback // 2):
        return None
    value = statistics.pstdev(values)
    return value if value > 1e-12 else None


def _candidate_for_symbol(
    source: FundingOiMarketSource,
    symbol: str,
    *,
    now_ms: int,
) -> tuple[_Candidate, int] | None:
    candles = _closed_daily(source.get_klines(symbol, "D", limit=260), now_ms=now_ms)
    if len(candles) < 201 or len(candles) < MOMENTUM_DAYS + 1:
        return None
    signal = candles[-1]
    signal_day = _utc_day(signal.start_time_ms)
    momentum_base = candles[-(MOMENTUM_DAYS + 1)].close
    if momentum_base <= 0:
        return None
    momentum = signal.close / momentum_base - Decimal(1)

    funding = _daily_funding(source.get_funding_history(symbol, limit=240))
    window_start = signal_day - timedelta(days=FUNDING_LOOKBACK_DAYS - 1)
    values = [
        float(value)
        for day, value in sorted(funding.items())
        if window_start <= day <= signal_day
    ]
    today = funding.get(signal_day)
    if today is None or len(values) < MIN_FUNDING_DAYS:
        return None
    mean = statistics.fmean(values)
    std = statistics.pstdev(values)
    if std <= 1e-12:
        return None
    z_score = (float(today) - mean) / std

    direction: TradeDirection | None = None
    if z_score <= -FUNDING_Z_THRESHOLD and momentum > 0:
        direction = TradeDirection.LONG
    elif z_score >= FUNDING_Z_THRESHOLD and momentum < 0:
        direction = TradeDirection.SHORT
    if direction is None:
        return None

    vol = _daily_vol(candles, INVOL_LOOKBACK)
    if vol is None:
        return None
    return (
        _Candidate(
            symbol=symbol,
            direction=direction,
            funding_z=z_score,
            momentum_3d=momentum,
            daily_vol=vol,
        ),
        signal.start_time_ms,
    )


def build_funding_oi_decision(
    source: FundingOiMarketSource,
    universe: tuple[str, ...],
    oi_growth: dict[str, Decimal],
    *,
    now_ms: int,
) -> FundingOiDecision:
    candidates: list[tuple[_Candidate, int]] = []
    for raw_symbol in universe:
        symbol = raw_symbol.upper()
        result = _candidate_for_symbol(source, symbol, now_ms=now_ms)
        if result is not None:
            candidates.append(result)

    if not candidates:
        return FundingOiDecision(0, None, ())

    candidates.sort(key=lambda item: (-abs(item[0].funding_z), item[0].symbol))
    chosen = candidates[:MAX_CANDIDATES]
    inverse_vol = {candidate.symbol: 1.0 / candidate.daily_vol for candidate, _ in chosen}
    total = sum(inverse_vol.values())
    if total <= 0:
        return FundingOiDecision(max(as_of for _, as_of in chosen), None, ())

    legs: list[FundingOiLeg] = []
    for candidate, _ in chosen:
        growth = oi_growth.get(candidate.symbol)
        if growth is None or growth < OI_GROWTH_THRESHOLD:
            continue
        # Deliberately do not renormalize after the OI filter. This is the conservative
        # finalist rule: rejected legs become cash instead of concentrating survivors.
        weight = inverse_vol[candidate.symbol] / total
        risk_fraction = SLEEVE_STOP_RISK_BUDGET * Decimal(str(weight))
        legs.append(
            FundingOiLeg(
                symbol=candidate.symbol,
                direction=candidate.direction,
                funding_z=Decimal(str(candidate.funding_z)),
                momentum_3d=candidate.momentum_3d,
                oi_growth=growth,
                target_weight=Decimal(str(weight)),
                risk_fraction=risk_fraction,
            )
        )

    as_of_ms = max(as_of for _, as_of in chosen)
    signal_day = _utc_day(as_of_ms).isoformat()
    legs.sort(key=lambda leg: (-abs(leg.funding_z), leg.symbol))
    return FundingOiDecision(as_of_ms, signal_day, tuple(legs))


def filter_funding_oi_candidates(
    candidates: tuple[DiscoveryResult, ...],
    decision: FundingOiDecision,
) -> tuple[DiscoveryResult, ...]:
    allowed = {(leg.symbol, leg.direction) for leg in decision.legs}
    rank = {(leg.symbol, leg.direction): index for index, leg in enumerate(decision.legs)}
    selected = [
        candidate
        for candidate in candidates
        if (candidate.symbol, candidate.direction) in allowed
    ]
    selected.sort(key=lambda item: rank[(item.symbol, item.direction)])
    return tuple(selected)


def total_sleeve_risk(decision: FundingOiDecision) -> Decimal:
    total = sum((leg.risk_fraction for leg in decision.legs), Decimal(0))
    if total > SLEEVE_STOP_RISK_BUDGET + Decimal(str(math.ulp(float(total or 1)))):
        raise ValueError("Funding+OI sleeve risk exceeds its hard budget")
    return total
