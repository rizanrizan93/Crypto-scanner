from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from crypto_scanner.binance.models import Candle
from crypto_scanner.market_neutral_research import (
    FIXED_UNIVERSE,
    FROZEN_CANDIDATES,
    DailyBar,
    RelativeValueCandidate,
    _renormalize_neutral,
    _signed_weights,
    candles_to_complete_utc_days,
    select_sides,
    simulate,
    split_calendar,
    summarize,
)


def _candle(day: date, hour: int, price: float) -> Candle:
    stamp = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
    value = Decimal(str(price))
    return Candle(
        start_time_ms=int(stamp.timestamp() * 1000),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("1"),
        turnover=value,
    )


def test_fixed_universe_matches_contract_and_excludes_fil():
    assert len(FIXED_UNIVERSE) == 20
    assert FIXED_UNIVERSE[-1] == "XLMUSDT"
    assert "XLMUSDT" in FIXED_UNIVERSE
    assert "FILUSDT" not in FIXED_UNIVERSE


def test_candidate_family_is_frozen_before_oos():
    assert [candidate.name for candidate in FROZEN_CANDIDATES] == [
        "MNV_MOM_28D_TOP3_WEEKLY",
        "MNV_MOM_60D_TOP3_WEEKLY",
        "MNV_MOMVOL_28D_TOP3_WEEKLY",
        "MNV_REV_7D_TOP3_DAILY",
        "MNV_REV_14D_TOP3_3DAY",
        "MNV_REV_28D_TOP3_WEEKLY",
    ]


def test_only_complete_six_bar_utc_days_are_kept():
    first = date(2026, 1, 1)
    second = date(2026, 1, 2)
    complete = tuple(
        _candle(first, hour, 100.0 + hour)
        for hour in (0, 4, 8, 12, 16, 20)
    )
    partial = tuple(
        _candle(second, hour, 200.0 + hour)
        for hour in (0, 4, 8, 12, 16)
    )
    rows = candles_to_complete_utc_days(complete + partial)
    assert len(rows) == 1
    assert rows[0].day == first
    assert rows[0].open == 100.0
    assert rows[0].close == 120.0


def test_momentum_and_reversal_select_opposite_sides():
    start = date(2025, 1, 1)
    closes = {symbol: {} for symbol in FIXED_UNIVERSE}
    for offset in range(100):
        day = start + timedelta(days=offset)
        for rank, symbol in enumerate(FIXED_UNIVERSE, start=1):
            closes[symbol][day] = 100.0 + rank * offset * 0.2

    momentum = RelativeValueCandidate("MOM", "MOM", 28, 3, 7)
    reversal = RelativeValueCandidate("REV", "REV", 28, 3, 7)
    signal_day = start + timedelta(days=99)

    mom_long, mom_short = select_sides(closes, signal_day, momentum)
    rev_long, rev_short = select_sides(closes, signal_day, reversal)

    assert len(mom_long) == len(mom_short) == 3
    assert mom_long == rev_short
    assert mom_short == rev_long
    assert not (set(mom_long) & set(mom_short))


def test_neutral_weights_have_unit_gross_and_zero_net():
    raw = _signed_weights(
        ("BTCUSDT", "ETHUSDT", "SOLUSDT"),
        ("XRPUSDT", "DOGEUSDT", "ADAUSDT"),
    )
    weights = _renormalize_neutral(raw)
    assert abs(sum(weights.values())) < 1e-12
    assert abs(sum(abs(value) for value in weights.values()) - 1.0) < 1e-12
    assert abs(sum(value for value in weights.values() if value > 0) - 0.5) < 1e-12
    assert abs(sum(value for value in weights.values() if value < 0) + 0.5) < 1e-12


def test_stress_cost_never_improves_daily_return():
    start = date(2024, 1, 1)
    daily = {}
    for rank, symbol in enumerate(FIXED_UNIVERSE, start=1):
        rows = []
        for offset in range(160):
            price = 100.0 + rank * offset * 0.1
            rows.append(DailyBar(start + timedelta(days=offset), price, price))
        daily[symbol] = tuple(rows)

    candidate = RelativeValueCandidate("TEST", "MOM", 28, 3, 7)
    rows = simulate(daily, candidate)
    assert rows
    assert all(row.stress_return <= row.base_return for row in rows)
    active = [row for row in rows if row.gross_exposure > 0]
    assert active
    assert all(abs(row.gross_exposure - 1.0) < 1e-12 for row in active)


def test_calendar_split_and_bootstrap_summary_are_deterministic():
    start = date(2023, 1, 1)
    daily = {}
    for rank, symbol in enumerate(FIXED_UNIVERSE, start=1):
        rows = []
        for offset in range(1400):
            price = 100.0 + rank * offset * 0.03
            rows.append(DailyBar(start + timedelta(days=offset), price, price))
        daily[symbol] = tuple(rows)

    candidate = RelativeValueCandidate("TEST", "MOM", 28, 3, 7)
    portfolio = simulate(daily, candidate)
    split = split_calendar(portfolio)
    assert len(split["train"]) >= 300
    assert len(split["validation"]) >= 300
    assert len(split["oos"]) >= 200
    first = summarize(split["validation"], field="stress_return")
    second = summarize(split["validation"], field="stress_return")
    assert first == second
    assert 0.0 <= float(first["bootstrap_positive_fraction_200"]) <= 1.0
