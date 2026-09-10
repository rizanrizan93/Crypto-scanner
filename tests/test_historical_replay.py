from decimal import Decimal

import pytest

from crypto_scanner.binance.models import Candle
from crypto_scanner.historical_replay import detect_impulse_retest, replay_fixed_geometry


def _candle(i: int, o: str, h: str, l: str, c: str) -> Candle:
    return Candle(
        start_time_ms=i * 60_000,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(c),
        volume=Decimal("100"),
        turnover=Decimal("10000"),
    )


def _base_series() -> list[Candle]:
    rows: list[Candle] = []
    price = Decimal("100")
    for i in range(40):
        next_price = price + Decimal("0.05")
        rows.append(
            _candle(
                i,
                str(price),
                str(next_price + Decimal("0.10")),
                str(price - Decimal("0.10")),
                str(next_price),
            )
        )
        price = next_price
    return rows


def _bullish_impulse_rows() -> tuple[list[Candle], Decimal]:
    rows = _base_series()
    prior_high = max(c.high for c in rows[-8:])
    rows.append(
        _candle(
            40,
            str(rows[-1].close),
            str(prior_high + Decimal("2.50")),
            str(rows[-1].close - Decimal("0.05")),
            str(prior_high + Decimal("2.20")),
        )
    )
    return rows, prior_high


def test_detects_bullish_impulse_retest_without_future_data() -> None:
    rows, prior_high = _bullish_impulse_rows()
    rows.append(
        _candle(
            41,
            str(prior_high + Decimal("2.10")),
            str(prior_high + Decimal("2.20")),
            str(prior_high - Decimal("0.05")),
            str(prior_high + Decimal("0.20")),
        )
    )

    signal = detect_impulse_retest(
        tuple(rows),
        impulse_atr=Decimal("1.0"),
        retest_tolerance_atr=Decimal("0.50"),
    )

    assert signal is not None
    assert signal.direction == "LONG"
    assert signal.retest_index == len(rows) - 1


def test_rejects_retest_that_sweeps_too_deep() -> None:
    rows, prior_high = _bullish_impulse_rows()
    rows.append(
        _candle(
            41,
            str(prior_high + Decimal("2.00")),
            str(prior_high + Decimal("2.10")),
            str(prior_high - Decimal("5.00")),
            str(prior_high + Decimal("0.20")),
        )
    )

    signal = detect_impulse_retest(
        tuple(rows),
        impulse_atr=Decimal("1.0"),
        retest_tolerance_atr=Decimal("0.50"),
    )

    assert signal is None


def test_rejects_setup_if_level_was_invalidated_before_decision() -> None:
    rows, prior_high = _bullish_impulse_rows()
    rows.append(
        _candle(
            41,
            str(prior_high + Decimal("0.10")),
            str(prior_high + Decimal("0.20")),
            str(prior_high - Decimal("3.00")),
            str(prior_high - Decimal("2.00")),
        )
    )
    rows.append(
        _candle(
            42,
            str(prior_high + Decimal("0.10")),
            str(prior_high + Decimal("0.30")),
            str(prior_high - Decimal("0.05")),
            str(prior_high + Decimal("0.20")),
        )
    )

    signal = detect_impulse_retest(
        tuple(rows),
        impulse_atr=Decimal("1.0"),
        retest_tolerance_atr=Decimal("0.50"),
    )

    assert signal is None


def test_replay_uses_conservative_stop_first_when_same_bar_hits_both() -> None:
    candles = (_candle(1, "100", "103", "97", "101"),)
    result = replay_fixed_geometry(
        candles,
        direction="LONG",
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        take_profit=Decimal("102"),
    )

    assert result.exit_reason == "SL"
    assert result.result_r == Decimal("-1")


def test_replay_short_tp_reports_r_multiple() -> None:
    candles = (_candle(1, "100", "100.5", "96", "97"),)
    result = replay_fixed_geometry(
        candles,
        direction="SHORT",
        entry_price=Decimal("100"),
        stop_loss=Decimal("102"),
        take_profit=Decimal("96"),
    )

    assert result.exit_reason == "TP"
    assert result.result_r == Decimal("2")


def test_replay_rejects_invalid_directional_geometry() -> None:
    with pytest.raises(ValueError, match="LONG geometry"):
        replay_fixed_geometry(
            (),
            direction="LONG",
            entry_price=Decimal("100"),
            stop_loss=Decimal("102"),
            take_profit=Decimal("104"),
        )
