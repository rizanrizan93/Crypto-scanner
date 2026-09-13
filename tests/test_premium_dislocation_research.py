from crypto_scanner.funding_carry_research import FundingPoint
from crypto_scanner.premium_dislocation_engine import Candidate, simulate_symbol
from crypto_scanner.premium_dislocation_signal import Signal


def test_long_and_short_price_symmetry() -> None:
    entry = 1_800_000_000_000
    exit_at = entry + 8 * 3_600_000
    opens = {entry: 100.0, exit_at: 110.0}
    candidate = Candidate("T", 8)
    long_trade = simulate_symbol("BTCUSDT", (Signal(entry, 1),), opens, (), candidate)[0]
    short_trade = simulate_symbol("BTCUSDT", (Signal(entry, -1),), opens, (), candidate)[0]
    assert long_trade.price_return == -short_trade.price_return


def test_positive_funding_benefits_short() -> None:
    entry = 1_800_000_000_000
    exit_at = entry + 8 * 3_600_000
    funding = (FundingPoint(entry + 4 * 3_600_000, 0.001),)
    opens = {entry: 100.0, exit_at: 100.0}
    candidate = Candidate("T", 8)
    long_trade = simulate_symbol("BTCUSDT", (Signal(entry, 1),), opens, funding, candidate)[0]
    short_trade = simulate_symbol("BTCUSDT", (Signal(entry, -1),), opens, funding, candidate)[0]
    assert long_trade.funding_return < 0
    assert short_trade.funding_return > 0


def test_overlap_is_skipped() -> None:
    entry = 1_800_000_000_000
    exit_at = entry + 8 * 3_600_000
    opens = {entry: 100.0, exit_at: 101.0}
    signals = (Signal(entry, 1), Signal(entry + 3_600_000, -1))
    trades = simulate_symbol("BTCUSDT", signals, opens, (), Candidate("T", 8))
    assert len(trades) == 1
