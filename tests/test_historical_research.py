from decimal import Decimal

from crypto_scanner.binance.models import Candle
from crypto_scanner.historical_research import (
    HistoricalResearchTrade,
    replay_impulse_retest_research,
    summarize_historical_research,
)


def _candle(i: int, o: str, h: str, low: str, c: str) -> Candle:
    return Candle(
        start_time_ms=i * 60_000,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal("100"),
        turnover=Decimal("10000"),
    )


def _series_with_bullish_retest() -> tuple[Candle, ...]:
    rows: list[Candle] = []
    price = Decimal("100")
    for i in range(40):
        nxt = price + Decimal("0.05")
        rows.append(
            _candle(
                i,
                str(price),
                str(nxt + Decimal("0.10")),
                str(price - Decimal("0.10")),
                str(nxt),
            )
        )
        price = nxt
    level = max(c.high for c in rows[-8:])
    rows.append(
        _candle(
            40,
            str(rows[-1].close),
            str(level + Decimal("2.50")),
            str(rows[-1].close - Decimal("0.05")),
            str(level + Decimal("2.20")),
        )
    )
    rows.append(
        _candle(
            41,
            str(level - Decimal("0.02")),
            str(level + Decimal("0.30")),
            str(level - Decimal("0.05")),
            str(level + Decimal("0.20")),
        )
    )
    rows.append(
        _candle(
            42,
            str(level + Decimal("0.20")),
            str(level + Decimal("0.80")),
            str(level + Decimal("0.10")),
            str(level + Decimal("0.70")),
        )
    )
    for i in range(43, 70):
        base = rows[-1].close
        rows.append(
            _candle(
                i,
                str(base),
                str(base + Decimal("0.50")),
                str(base - Decimal("0.05")),
                str(base + Decimal("0.30")),
            )
        )
    return tuple(rows)


def test_research_enters_only_after_decision_candle() -> None:
    candles = _series_with_bullish_retest()
    trades = replay_impulse_retest_research(
        candles,
        horizon_bars=10,
        impulse_atr=Decimal("1.0"),
        retest_tolerance_atr=Decimal("0.50"),
    )

    assert trades
    first = trades[0]
    assert first.direction == "LONG"
    assert first.decision_time_ms == candles[41].start_time_ms
    assert first.entry_time_ms == candles[42].start_time_ms
    assert first.entry_price == candles[42].open


def test_research_deduplicates_same_impulse() -> None:
    candles = _series_with_bullish_retest()
    trades = replay_impulse_retest_research(
        candles,
        horizon_bars=10,
        impulse_atr=Decimal("1.0"),
        retest_tolerance_atr=Decimal("0.50"),
    )

    impulse_ids = [trade.impulse_index for trade in trades]
    assert len(impulse_ids) == len(set(impulse_ids))


def test_summary_uses_net_r_and_profit_factor() -> None:
    trades = (
        HistoricalResearchTrade(
            decision_time_ms=1,
            entry_time_ms=2,
            direction="LONG",
            impulse_index=1,
            entry_price=Decimal("100"),
            stop_loss=Decimal("99"),
            take_profit=Decimal("102"),
            gross_result_r=Decimal("2"),
            net_result_r=Decimal("1.9"),
            mfe_r=Decimal("2"),
            mae_r=Decimal("0.2"),
            exit_reason="TP",
        ),
        HistoricalResearchTrade(
            decision_time_ms=3,
            entry_time_ms=4,
            direction="LONG",
            impulse_index=2,
            entry_price=Decimal("100"),
            stop_loss=Decimal("99"),
            take_profit=Decimal("102"),
            gross_result_r=Decimal("-1"),
            net_result_r=Decimal("-1.1"),
            mfe_r=Decimal("0.1"),
            mae_r=Decimal("1"),
            exit_reason="SL",
        ),
    )

    summary = summarize_historical_research(trades)

    assert summary.sample_size == 2
    assert summary.win_rate == Decimal("0.5")
    assert summary.average_net_r == Decimal("0.4")
    assert summary.profit_factor_r == Decimal("1.9") / Decimal("1.1")
