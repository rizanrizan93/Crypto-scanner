from datetime import UTC, datetime, timedelta

from crypto_scanner.adaptive_regime_signal import RegimePoint
from crypto_scanner.regime_leader_laggard_core import DayBar, build_weekly_trades


def make_days(multiplier: float) -> tuple[DayBar, ...]:
    start = datetime(2024, 12, 9, tzinfo=UTC)
    rows = []
    for idx in range(40):
        ts = int((start + timedelta(days=idx)).timestamp() * 1000)
        price = 100.0 * (1.0 + multiplier * idx)
        rows.append(DayBar(ts, price, price))
    return tuple(rows)


def test_bull_selects_top_three_and_bear_bottom_three() -> None:
    daily = {
        "BTCUSDT": make_days(0.001),
        "AUSDT": make_days(0.005),
        "BUSDT": make_days(0.004),
        "CUSDT": make_days(0.003),
        "DUSDT": make_days(-0.001),
        "EUSDT": make_days(-0.002),
    }
    funding = {symbol: () for symbol in daily}
    entry_ms = daily["BTCUSDT"][29].start_ms
    bull = (RegimePoint(entry_ms, 1),)
    bear = (RegimePoint(entry_ms, -1),)
    bull_trade = build_weekly_trades(daily, funding, bull)[0]
    bear_trade = build_weekly_trades(daily, funding, bear)[0]
    assert bull_trade.regime == 1
    assert bull_trade.symbols == ("CUSDT", "BUSDT", "AUSDT")
    assert bear_trade.regime == -1
    assert bear_trade.symbols == ("EUSDT", "DUSDT", "BTCUSDT")


def test_flat_regime_stays_cash() -> None:
    daily = {"BTCUSDT": make_days(0.001), "AUSDT": make_days(0.002), "BUSDT": make_days(0.003)}
    funding = {symbol: () for symbol in daily}
    entry_ms = daily["BTCUSDT"][29].start_ms
    assert build_weekly_trades(daily, funding, (RegimePoint(entry_ms, 0),)) == ()
