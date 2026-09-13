from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from crypto_scanner.binance.models import Candle
from crypto_scanner.cross_sectional_momentum_research import (
    FROZEN_CANDIDATES,
    DailyBar,
    MomentumCandidate,
    candles_to_complete_utc_days,
    select_holdings,
    simulate_portfolio,
    split_calendar,
    summarize,
)


def _candle(day: date, hour: int, price: str) -> Candle:
    stamp = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
    value = Decimal(price)
    return Candle(
        start_time_ms=int(stamp.timestamp() * 1000),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("1"),
        turnover=value,
    )


def test_frozen_candidate_registry_is_small_and_predeclared():
    assert [candidate.name for candidate in FROZEN_CANDIDATES] == [
        "XSMOM_14D_TOP3_DAILY",
        "XSMOM_28D_TOP3_DAILY",
        "XSMOM_28D_TOP5_DAILY",
        "XSMOM_28D_TOP3_WEEKLY",
        "XSMOM_60D_TOP3_WEEKLY_BTC",
    ]


def test_only_complete_utc_days_are_kept():
    first = date(2026, 1, 1)
    second = date(2026, 1, 2)
    rows = tuple(
        _candle(first, hour, str(100 + hour))
        for hour in (0, 4, 8, 12, 16, 20)
    )
    rows += tuple(
        _candle(second, hour, str(200 + hour))
        for hour in (0, 4, 8, 12, 16)
    )
    daily = candles_to_complete_utc_days(rows)
    assert len(daily) == 1
    assert daily[0].day == first
    assert daily[0].open == 100.0
    assert daily[0].close == 120.0


def test_relative_strength_selects_only_positive_trending_leaders():
    start = date(2025, 1, 1)
    closes = {"AAAUSDT": {}, "BBBUSDT": {}, "CCCUSDT": {}}
    for offset in range(80):
        day = start + timedelta(days=offset)
        closes["AAAUSDT"][day] = 100.0 + 2.0 * offset
        closes["BBBUSDT"][day] = 100.0 + 1.0 * offset
        closes["CCCUSDT"][day] = 200.0 - 0.5 * offset
    candidate = MomentumCandidate(
        "TEST",
        lookback_days=14,
        top_k=2,
        rebalance_days=1,
        ema_days=20,
    )
    picked = select_holdings(closes, start + timedelta(days=79), candidate)
    assert picked == ("AAAUSDT", "BBBUSDT")


def test_stress_cost_never_improves_portfolio_return():
    start = date(2024, 1, 1)
    daily = {}
    for symbol, slope in (("AAAUSDT", 1.5), ("BBBUSDT", 1.0), ("BTCUSDT", 0.8)):
        rows = []
        for offset in range(180):
            price = 100.0 + slope * offset
            rows.append(
                DailyBar(
                    start + timedelta(days=offset),
                    price,
                    price + slope * 0.5,
                )
            )
        daily[symbol] = tuple(rows)
    candidate = MomentumCandidate(
        "TEST",
        lookback_days=14,
        top_k=2,
        rebalance_days=1,
        ema_days=20,
    )
    rows = simulate_portfolio(daily, candidate)
    assert rows
    assert all(row.stress_return <= row.base_return for row in rows)


def test_calendar_split_and_summary_are_deterministic():
    start = date(2023, 1, 1)
    rows = []
    for offset in range(1400):
        day = start + timedelta(days=offset)
        rows.append(DailyBar(day, 100.0 + offset, 100.5 + offset))
    candidate = MomentumCandidate(
        "TEST",
        lookback_days=14,
        top_k=1,
        rebalance_days=1,
        ema_days=20,
    )
    portfolio = simulate_portfolio({"BTCUSDT": tuple(rows)}, candidate)
    split = split_calendar(portfolio)
    assert len(split["train"]) >= 300
    assert len(split["validation"]) >= 300
    assert len(split["oos"]) > 0
    first = summarize(split["validation"], field="stress_return")
    second = summarize(split["validation"], field="stress_return")
    assert first == second
    assert 0.0 <= float(first["bootstrap_positive_fraction_100"]) <= 1.0
