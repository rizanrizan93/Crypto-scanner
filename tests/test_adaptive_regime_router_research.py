from decimal import Decimal

from crypto_scanner.adaptive_regime_backtest import simulate_price
from crypto_scanner.adaptive_regime_signal import H4_MS, RegimePoint, Setup, build_setups
from crypto_scanner.binance.models import Candle


def c(i: int, o: str, h: str, l: str, close: str) -> Candle:
    return Candle(start_time_ms=i * H4_MS, open=Decimal(o), high=Decimal(h), low=Decimal(l), close=Decimal(close), volume=Decimal("1"), turnover=Decimal("1"))


def test_bull_routes_long_and_bear_routes_short() -> None:
    rows = tuple(c(i, str(100 + i * 0.1), str(101 + i * 0.1), str(99 + i * 0.1), str(100.5 + i * 0.1)) for i in range(70))
    bull = (RegimePoint(0, 1),)
    bear = (RegimePoint(0, -1),)
    assert all(x.direction == 1 for x in build_setups(rows, bull))
    assert all(x.direction == -1 for x in build_setups(rows, bear))


def test_stop_first_collision() -> None:
    rows = [c(i, "100", "101", "99", "100") for i in range(20)]
    rows[1] = c(1, "100", "104", "98", "101")
    trade = simulate_price("BTCUSDT", tuple(rows), (Setup(0, 1, 1.0),))[0]
    assert trade.exit_reason == "STOP"
    assert abs(trade.gross_r + 1.0) < 1e-9


def test_next_bar_entry() -> None:
    rows = [c(i, "100", "101", "99", "100") for i in range(20)]
    rows[1] = c(1, "110", "111", "108", "109")
    trade = simulate_price("BTCUSDT", tuple(rows), (Setup(0, 1, 1.0),))[0]
    assert trade.entry_ms == H4_MS
