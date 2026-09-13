from crypto_scanner.btc_xgb_data import FundingPoint
from crypto_scanner.btc_xgb_features import Sample
from crypto_scanner.run_btc_xgb_research import Candidate, Prediction, build_trades


def sample(entry: int, target: float) -> Sample:
    return Sample(
        signal_time_ms=entry - 3_600_000,
        entry_time_ms=entry,
        exit_time_ms=entry + 4 * 3_600_000,
        entry_price=100.0,
        exit_price=100.0 * (1.0 + target),
        features=(0.0,) * 17,
        target_return=target,
    )


def test_forecast_sign_controls_long_and_short() -> None:
    long_prediction = Prediction(sample(10_000_000, 0.01), 0.005)
    short_prediction = Prediction(sample(30_000_000, 0.01), -0.005)
    trades = build_trades(
        (long_prediction, short_prediction),
        (),
        Candidate("T", 0.0021),
    )
    assert trades[0].direction == 1
    assert trades[1].direction == -1
    assert trades[0].price_return == -trades[1].price_return


def test_no_trade_inside_cost_filter() -> None:
    predictions = (Prediction(sample(10_000_000, 0.01), 0.001),)
    assert build_trades(predictions, (), Candidate("T", 0.0021)) == ()


def test_positive_funding_benefits_short() -> None:
    entry = 10_000_000
    prediction = Prediction(sample(entry, 0.0), -0.005)
    funding = (FundingPoint(entry + 2 * 3_600_000, 0.001),)
    trade = build_trades((prediction,), funding, Candidate("T", 0.0021))[0]
    assert trade.direction == -1
    assert trade.funding_return > 0


def test_overlap_is_skipped() -> None:
    first = Prediction(sample(10_000_000, 0.01), 0.005)
    second = Prediction(sample(10_000_000 + 3_600_000, 0.01), -0.005)
    trades = build_trades((first, second), (), Candidate("T", 0.0021))
    assert len(trades) == 1
