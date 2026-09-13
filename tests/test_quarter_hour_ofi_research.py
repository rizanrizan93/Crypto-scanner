from datetime import UTC, datetime, timedelta

from crypto_scanner.funding_carry_research import FundingPoint
from crypto_scanner.quarter_hour_ofi_core import Candidate, Signal, simulate_symbol


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


def test_long_and_short_are_symmetric() -> None:
    t0 = _ms("2026-01-01T00:00:00")
    entry = t0 + 60_000
    exit_at = entry + 4 * 3_600_000
    opens = {entry: 100.0, exit_at: 110.0}
    candidate = Candidate("T", 4)
    long_trade = simulate_symbol("BTCUSDT", (Signal(t0, 2.0),), opens, (), candidate)[0]
    short_trade = simulate_symbol("BTCUSDT", (Signal(t0, -2.0),), opens, (), candidate)[0]
    assert long_trade.direction == 1
    assert short_trade.direction == -1
    assert long_trade.price_return == -short_trade.price_return


def test_positive_funding_benefits_short() -> None:
    t0 = _ms("2026-01-01T00:00:00")
    entry = t0 + 60_000
    exit_at = entry + 4 * 3_600_000
    opens = {entry: 100.0, exit_at: 100.0}
    funding_time = entry + int(timedelta(hours=2).total_seconds() * 1000)
    funding = (FundingPoint(funding_time, 0.001),)
    candidate = Candidate("T", 4)
    long_trade = simulate_symbol("BTCUSDT", (Signal(t0, 2.0),), opens, funding, candidate)[0]
    short_trade = simulate_symbol("BTCUSDT", (Signal(t0, -2.0),), opens, funding, candidate)[0]
    assert long_trade.funding_return < 0
    assert short_trade.funding_return > 0


def test_overlapping_signal_is_skipped() -> None:
    t0 = _ms("2026-01-01T00:00:00")
    entry1 = t0 + 60_000
    exit1 = entry1 + 4 * 3_600_000
    t1 = t0 + 15 * 60_000
    opens = {entry1: 100.0, exit1: 101.0}
    trades = simulate_symbol(
        "BTCUSDT",
        (Signal(t0, 2.0), Signal(t1, -2.0)),
        opens,
        (),
        Candidate("T", 4),
    )
    assert len(trades) == 1
