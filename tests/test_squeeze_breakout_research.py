from decimal import Decimal

from crypto_scanner.binance.models import Candle
from crypto_scanner.squeeze_breakout_engine import simulate_symbol
from crypto_scanner.squeeze_breakout_signal import BAR_MS, Setup


def candle(index: int, open_: str, high: str, low: str, close: str) -> Candle:
    return Candle(
        start_time_ms=index * BAR_MS,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
        turnover=Decimal("1"),
    )


def test_stop_first_when_both_levels_touch() -> None:
    rows = [candle(i, "100", "101", "99", "100") for i in range(20)]
    rows[1] = candle(1, "100", "104", "98", "101")
    result = simulate_symbol("BTCUSDT", tuple(rows), (), (Setup(0, 1, 1.0),))[0]
    assert result.exit_reason == "STOP"
    assert abs(result.gross_r + 1.0) < 1e-9


def test_long_and_short_target_are_symmetric() -> None:
    long_rows = [candle(i, "100", "101", "99", "100") for i in range(20)]
    short_rows = [candle(i, "100", "101", "99", "100") for i in range(20)]
    long_rows[1] = candle(1, "100", "104", "99", "103")
    short_rows[1] = candle(1, "100", "101", "96", "97")
    long_result = simulate_symbol("BTCUSDT", tuple(long_rows), (), (Setup(0, 1, 1.0),))[0]
    short_result = simulate_symbol("BTCUSDT", tuple(short_rows), (), (Setup(0, -1, 1.0),))[0]
    assert long_result.exit_reason == "TARGET"
    assert short_result.exit_reason == "TARGET"
    assert abs(long_result.gross_r - 2.0) < 1e-9
    assert abs(short_result.gross_r - 2.0) < 1e-9


def test_entry_is_next_bar_open() -> None:
    rows = [candle(i, "100", "101", "99", "100") for i in range(20)]
    rows[1] = candle(1, "110", "111", "108", "109")
    result = simulate_symbol("BTCUSDT", tuple(rows), (), (Setup(0, 1, 1.0),))[0]
    assert result.entry_time_ms == BAR_MS
