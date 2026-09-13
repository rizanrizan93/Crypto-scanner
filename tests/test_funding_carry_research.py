import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from crypto_scanner.binance.models import Candle
from crypto_scanner.funding_carry_research import (
    FIXED_UNIVERSE,
    FROZEN_CANDIDATES,
    DailyBar,
    FundingCarryCandidate,
    FundingPoint,
    _renormalize_neutral,
    _stress_funding,
    _strict_intraday_funding_sum,
    _weights,
    candles_to_complete_utc_days,
    select_sides,
    simulate,
    split_calendar,
    summarize,
)
from crypto_scanner.run_funding_carry_research import _parse_funding_zip


def _funding(stamp: datetime, rate: float) -> FundingPoint:
    return FundingPoint(
        funding_time_ms=int(stamp.timestamp() * 1000),
        funding_rate=rate,
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


def test_fixed_universe_matches_contract():
    assert len(FIXED_UNIVERSE) == 20
    assert "XLMUSDT" in FIXED_UNIVERSE
    assert "FILUSDT" not in FIXED_UNIVERSE


def test_funding_candidates_are_frozen_before_oos():
    assert [candidate.name for candidate in FROZEN_CANDIDATES] == [
        "FCR_3D_TOP3_DAILY",
        "FCR_7D_TOP3_DAILY",
        "FCR_7D_TOP5_DAILY",
        "FCR_14D_TOP3_3DAY",
        "FCR_28D_TOP3_WEEKLY",
    ]


def test_binance_vision_funding_archive_schema_is_parsed():
    first = int(datetime(2026, 1, 1, 0, tzinfo=UTC).timestamp() * 1000)
    second = int(datetime(2026, 1, 1, 8, tzinfo=UTC).timestamp() * 1000)
    raw = (
        "calc_time,funding_interval_hours,last_funding_rate\n"
        f"{first},8,0.0001\n"
        f"{second},8,-0.0002\n"
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("BTCUSDT-fundingRate-2026-01.csv", raw)
    points = _parse_funding_zip(buffer.getvalue())
    assert [point.funding_time_ms for point in points] == [first, second]
    assert [point.funding_rate for point in points] == [0.0001, -0.0002]


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


def test_negative_funding_goes_long_and_positive_funding_goes_short():
    signal_day = date(2026, 1, 10)
    candidate = FundingCarryCandidate("TEST", 3, 3, 1)
    funding_by_symbol = {}
    for rank, symbol in enumerate(FIXED_UNIVERSE):
        rate = (rank - 10) * 0.0001
        points = []
        for day_offset in range(3):
            day = signal_day - timedelta(days=day_offset)
            for hour in (0, 8, 16):
                points.append(
                    _funding(
                        datetime(day.year, day.month, day.day, hour, tzinfo=UTC),
                        rate,
                    )
                )
        funding_by_symbol[symbol] = tuple(
            sorted(points, key=lambda point: point.funding_time_ms)
        )

    longs, shorts = select_sides(funding_by_symbol, signal_day, candidate)
    assert len(longs) == len(shorts) == 3
    assert set(longs) == set(FIXED_UNIVERSE[:3])
    assert set(shorts) == set(FIXED_UNIVERSE[-3:])


def test_funding_exactly_at_entry_and_exit_is_excluded():
    entry_day = date(2026, 1, 10)
    exit_day = date(2026, 1, 11)
    points = (
        _funding(datetime(2026, 1, 10, 0, tzinfo=UTC), 0.001),
        _funding(datetime(2026, 1, 10, 8, tzinfo=UTC), 0.002),
        _funding(datetime(2026, 1, 10, 16, tzinfo=UTC), 0.003),
        _funding(datetime(2026, 1, 11, 0, tzinfo=UTC), 0.004),
    )
    assert _strict_intraday_funding_sum(
        points,
        entry_day=entry_day,
        exit_day=exit_day,
    ) == 0.005


def test_stress_haircuts_benefit_and_magnifies_cost():
    assert _stress_funding(0.01) == 0.008
    assert _stress_funding(-0.01) == -0.012


def test_neutral_weights_have_zero_net_and_unit_gross():
    raw = _weights(
        ("BTCUSDT", "ETHUSDT", "SOLUSDT"),
        ("XRPUSDT", "DOGEUSDT", "ADAUSDT"),
    )
    weights = _renormalize_neutral(raw)
    assert abs(sum(weights.values())) < 1e-12
    assert abs(sum(abs(value) for value in weights.values()) - 1.0) < 1e-12


def test_stress_return_is_not_better_on_pure_funding_benefit_case():
    start = date(2024, 1, 1)
    daily = {}
    funding = {}
    for rank, symbol in enumerate(FIXED_UNIVERSE):
        daily[symbol] = tuple(
            DailyBar(start + timedelta(days=offset), 100.0, 100.0)
            for offset in range(90)
        )
        rate = (rank - 10) * 0.0001
        funding[symbol] = tuple(
            _funding(
                datetime.combine(
                    start + timedelta(days=offset),
                    datetime.min.time(),
                    tzinfo=UTC,
                )
                + timedelta(hours=hour),
                rate,
            )
            for offset in range(90)
            for hour in (0, 8, 16)
        )

    candidate = FundingCarryCandidate("TEST", 3, 3, 1)
    rows = simulate(daily, funding, candidate)
    active = [row for row in rows if row.gross_exposure > 0]
    assert active
    assert all(row.stress_return <= row.base_return for row in active)
    assert all(abs(row.gross_exposure - 1.0) < 1e-12 for row in active)


def test_split_and_summary_are_deterministic():
    start = date(2023, 1, 1)
    daily = {}
    funding = {}
    for rank, symbol in enumerate(FIXED_UNIVERSE):
        daily[symbol] = tuple(
            DailyBar(start + timedelta(days=offset), 100.0, 100.0)
            for offset in range(1400)
        )
        rate = (rank - 10) * 0.00005
        funding[symbol] = tuple(
            _funding(
                datetime.combine(
                    start + timedelta(days=offset),
                    datetime.min.time(),
                    tzinfo=UTC,
                )
                + timedelta(hours=hour),
                rate,
            )
            for offset in range(1400)
            for hour in (0, 8, 16)
        )

    candidate = FundingCarryCandidate("TEST", 7, 3, 7)
    portfolio = simulate(daily, funding, candidate)
    split = split_calendar(portfolio)
    assert len(split["train"]) >= 300
    assert len(split["validation"]) >= 300
    assert len(split["oos"]) >= 200
    first = summarize(split["validation"], field="stress_return")
    second = summarize(split["validation"], field="stress_return")
    assert first == second
    assert 0.0 <= float(first["bootstrap_positive_fraction_200"]) <= 1.0
