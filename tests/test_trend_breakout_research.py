from decimal import Decimal

from crypto_scanner.binance.models import Candle
from crypto_scanner.trend_breakout_research import (
    FROZEN_CANDIDATES,
    reprice_cost,
    replay_candidate,
    summarize,
)


def _candles():
    rows = []
    price = Decimal("100")
    for index in range(260):
        close = price + Decimal(index) * Decimal("0.03")
        rows.append(
            Candle(
                start_time_ms=1_700_000_000_000 + index * 3_600_000,
                open=close - Decimal("0.01"),
                high=close + Decimal("0.20"),
                low=close - Decimal("0.20"),
                close=close,
                volume=Decimal("1000"),
                turnover=Decimal("100000"),
            )
        )
    index = 220
    previous_high = max(row.high for row in rows[index - 20:index])
    breakout = previous_high + Decimal("3")
    rows[index] = Candle(
        start_time_ms=rows[index].start_time_ms,
        open=rows[index].open,
        high=breakout + Decimal("0.5"),
        low=rows[index].low,
        close=breakout,
        volume=Decimal("2000"),
        turnover=Decimal("200000"),
    )
    rows[index + 1] = Candle(
        start_time_ms=rows[index + 1].start_time_ms,
        open=breakout,
        high=breakout + Decimal("10"),
        low=breakout - Decimal("0.1"),
        close=breakout + Decimal("8"),
        volume=Decimal("2000"),
        turnover=Decimal("200000"),
    )
    return tuple(rows)


def test_frozen_candidates_are_preregistered():
    assert [row.name for row in FROZEN_CANDIDATES] == [
        "CRYPTO_DONCHIAN20_2R",
        "CRYPTO_DONCHIAN20_3R",
        "CRYPTO_DONCHIAN55_2R",
        "CRYPTO_COMP20_2R",
    ]


def test_breakout_replay_enters_after_decision_bar():
    rows = replay_candidate(
        _candles(),
        symbol="BTCUSDT",
        candidate=FROZEN_CANDIDATES[0],
    )
    assert rows
    first = rows[0]
    assert first.entry_time_ms > first.decision_time_ms
    assert first.stop_loss < first.entry_price
    assert first.take_profit > first.entry_price


def test_cost_repricing_reduces_net_result_without_changing_geometry():
    rows = replay_candidate(
        _candles(),
        symbol="BTCUSDT",
        candidate=FROZEN_CANDIDATES[0],
    )
    priced = reprice_cost(rows, round_trip_cost_bps=Decimal("14"))
    assert priced[0].entry_price == rows[0].entry_price
    assert priced[0].stop_loss == rows[0].stop_loss
    assert priced[0].gross_r == rows[0].gross_r
    assert priced[0].net_r < priced[0].gross_r


def test_summary_uses_net_r():
    rows = replay_candidate(
        _candles(),
        symbol="BTCUSDT",
        candidate=FROZEN_CANDIDATES[0],
    )
    summary = summarize(reprice_cost(rows, round_trip_cost_bps=Decimal("8")))
    assert summary.trades == len(rows)
    assert summary.max_drawdown_r >= 0
