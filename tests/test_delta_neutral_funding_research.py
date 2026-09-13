from datetime import UTC, date, datetime, timedelta

from crypto_scanner.delta_neutral_funding_research import (
    CORE5,
    FROZEN_CANDIDATES,
    Candidate,
    FundingPoint,
    _norm_ms,
    passes,
    simulate,
)


def test_frozen_family_is_small_and_preregistered():
    assert CORE5 == ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")
    assert [row.name for row in FROZEN_CANDIDATES] == [
        "DNC_BTC_7D_WEEKLY",
        "DNC_BTCETH_7D_WEEKLY",
        "DNC_CORE5_7D_TOP3_WEEKLY",
        "DNC_CORE5_14D_TOP3_WEEKLY",
        "DNC_CORE5_28D_TOP3_WEEKLY",
    ]


def test_spot_microsecond_epoch_normalizes_to_milliseconds():
    ms = 1_735_689_600_000
    assert _norm_ms(ms) == ms
    assert _norm_ms(ms * 1000) == ms


def test_equal_spot_and_perp_price_moves_cancel_directional_beta():
    start = date(2023, 1, 1)
    spot = {}
    perp = {}
    funding = []
    for i in range(40):
        day = start + timedelta(days=i)
        px = 100.0 * (1.01**i)
        spot[day] = px
        perp[day] = px
        for hour in (8, 16):
            stamp = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
            funding.append(FundingPoint(int(stamp.timestamp() * 1000), 0.0001))
    candidate = Candidate("TEST", ("BTCUSDT",), 7, 1, 7)
    rows = simulate({"BTCUSDT": (spot, perp, tuple(funding))}, candidate)
    active = [row for row in rows if row.gross_exposure > 0 and row.turnover == 0]
    assert active
    assert all(abs(row.basis_return) < 1e-12 for row in active)
    assert all(row.base_funding_return > 0 for row in active)
    assert all(row.stress_funding_return > 0 for row in active)


def test_gate_rejects_negative_train_even_if_validation_and_oos_are_good():
    good = {
        "days": 365,
        "total_return": 0.10,
        "annualized_sharpe": 1.0,
        "max_drawdown": 0.05,
        "avg_gross_exposure": 0.8,
        "bootstrap_positive_fraction_200": 0.9,
    }
    oos = dict(good, days=242)
    bad_train = dict(good, total_return=-0.01)
    record = {"stress": {"train": bad_train, "validation": good, "oos": oos}}
    assert passes(record) is False
