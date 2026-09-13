from datetime import date

from crypto_scanner.slow_trend_ensemble_research import (
    FROZEN_CANDIDATES,
    Bar,
    _signal_series,
    passes,
)


def test_candidate_grid_is_frozen_and_small():
    assert [candidate.name for candidate in FROZEN_CANDIDATES] == [
        "STE_DONCHIAN_20_60_120",
        "STE_DONCHIAN_20_55_100",
        "STE_DONCHIAN_30_90_180",
    ]


def test_donchian_signal_uses_only_prior_window():
    bars = {}
    for index in range(25):
        day = date(2024, 1, 1).fromordinal(date(2024, 1, 1).toordinal() + index)
        close = 100.0 + index
        bars[day] = Bar(day, close, close + 0.5, close - 0.5, close)
    signals = _signal_series(bars, (5, 10, 20))
    assert signals[max(bars)] > 0


def test_gate_requires_positive_train():
    good = {
        "days": 365,
        "total_return": 0.10,
        "annualized_sharpe": 1.0,
        "max_drawdown": 0.10,
        "avg_gross_exposure": 1.0,
        "bootstrap_positive_fraction_200": 0.9,
    }
    oos = dict(good, days=242)
    bad_train = dict(good, total_return=-0.01)
    assert passes({"stress": {"train": bad_train, "validation": good, "oos": oos}}) is False
