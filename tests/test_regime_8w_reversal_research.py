from datetime import UTC, datetime, timedelta

from crypto_scanner.adaptive_regime_signal import RegimePoint
from crypto_scanner.regime_8w_reversal_core import build_8w_reversal
from crypto_scanner.regime_leader_laggard_core import DayBar


def make_days(rate: float) -> tuple[DayBar, ...]:
    start = datetime(2024, 10, 7, tzinfo=UTC)
    rows = []
    for idx in range(80):
        ts = int((start + timedelta(days=idx)).timestamp() * 1000)
        price = 100.0 * (1.0 + rate * idx)
        rows.append(DayBar(ts, price, price))
    return tuple(rows)


def test_bull_longs_8w_losers_and_bear_shorts_8w_winners() -> None:
    daily = {
        "BTCUSDT": make_days(0.001),
        "AUSDT": make_days(0.005),
        "BUSDT": make_days(0.004),
        "CUSDT": make_days(0.003),
        "DUSDT": make_days(-0.001),
        "EUSDT": make_days(-0.002),
    }
    funding = {symbol: () for symbol in daily}
    entry_ms = daily["BTCUSDT"][57].start_ms
    bull = build_8w_reversal(daily, funding, (RegimePoint(entry_ms, 1),))[0]
    bear = build_8w_reversal(daily, funding, (RegimePoint(entry_ms, -1),))[0]
    assert bull.symbols == ("EUSDT", "DUSDT", "BTCUSDT")
    assert bear.symbols == ("CUSDT", "BUSDT", "AUSDT")
