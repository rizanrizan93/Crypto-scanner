from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from crypto_scanner.binance.models import Candle, FundingRatePoint
from crypto_scanner.discovery import TradeDirection
from crypto_scanner.funding_oi_demo import (
    SLEEVE_STOP_RISK_BUDGET,
    build_funding_oi_decision,
    total_sleeve_risk,
)
from crypto_scanner.funding_oi_state import FundingOiSnapshotState

DAY_MS = 86_400_000


class FakeFundingSource:
    def __init__(self, candles: tuple[Candle, ...], funding: tuple[FundingRatePoint, ...]):
        self.candles = candles
        self.funding = funding

    def get_klines(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int = 200,
    ) -> tuple[Candle, ...]:
        assert interval == "D"
        return self.candles[-limit:]

    def get_funding_history(
        self,
        symbol: str,
        *,
        limit: int = 50,
    ) -> tuple[FundingRatePoint, ...]:
        return self.funding[-limit:]


def _market(*, falling: bool, funding_spike: Decimal) -> FakeFundingSource:
    start = date(2026, 1, 1)
    price = Decimal("100")
    candles: list[Candle] = []
    for index in range(240):
        day = start + timedelta(days=index)
        timestamp = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
        if index >= 237:
            price *= Decimal("0.98") if falling else Decimal("1.02")
        else:
            price *= Decimal("1.004") if index % 3 else Decimal("0.993")
        candles.append(
            Candle(
                start_time_ms=timestamp,
                open=price,
                high=price * Decimal("1.01"),
                low=price * Decimal("0.99"),
                close=price,
                volume=Decimal("1000"),
                turnover=Decimal("100000"),
            )
        )

    signal_day = start + timedelta(days=239)
    funding: list[FundingRatePoint] = []
    for offset in range(60):
        day = signal_day - timedelta(days=59 - offset)
        timestamp = int(
            datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp() * 1000
        )
        rate = funding_spike if offset == 59 else Decimal("0.0001")
        funding.append(FundingRatePoint(timestamp_ms=timestamp, funding_rate=rate))
    return FakeFundingSource(tuple(candles), tuple(funding))


def test_positive_funding_dislocation_with_falling_price_builds_short_leg() -> None:
    source = _market(falling=True, funding_spike=Decimal("0.01"))
    now_ms = source.candles[-1].start_time_ms + DAY_MS + 1

    decision = build_funding_oi_decision(
        source,
        ("BTCUSDT",),
        {"BTCUSDT": Decimal("0.03")},
        now_ms=now_ms,
    )

    assert len(decision.legs) == 1
    assert decision.legs[0].direction is TradeDirection.SHORT
    assert decision.legs[0].funding_z >= Decimal("2")
    assert decision.legs[0].oi_growth == Decimal("0.03")
    assert total_sleeve_risk(decision) <= SLEEVE_STOP_RISK_BUDGET


def test_oi_growth_below_two_percent_keeps_dislocation_in_cash() -> None:
    source = _market(falling=True, funding_spike=Decimal("0.01"))
    now_ms = source.candles[-1].start_time_ms + DAY_MS + 1

    decision = build_funding_oi_decision(
        source,
        ("BTCUSDT",),
        {"BTCUSDT": Decimal("0.019")},
        now_ms=now_ms,
    )

    assert decision.legs == ()
    assert total_sleeve_risk(decision) == 0


def test_daily_oi_growth_requires_consecutive_snapshot_days() -> None:
    state = FundingOiSnapshotState(
        version=2,
        current_day=date(2026, 9, 14),
        current_values={"BTCUSDT": Decimal("102")},
        previous_day=date(2026, 9, 13),
        previous_values={"BTCUSDT": Decimal("100")},
    )
    assert state.growth()["BTCUSDT"] == Decimal("0.02")

    stale = FundingOiSnapshotState(
        version=2,
        current_day=date(2026, 9, 14),
        current_values={"BTCUSDT": Decimal("102")},
        previous_day=date(2026, 9, 12),
        previous_values={"BTCUSDT": Decimal("100")},
    )
    assert stale.growth() == {}
