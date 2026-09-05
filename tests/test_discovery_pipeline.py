from __future__ import annotations

from decimal import Decimal

from crypto_scanner.bybit.models import Candle, OpenInterestPoint, TickerSnapshot
from crypto_scanner.discovery_pipeline import DiscoveryPipeline

NOW_MS = 2_000_000_000_000


class FakePublicClient:
    def __init__(self, *, stale: bool = False) -> None:
        self.stale = stale

    def get_ticker(self, symbol: str) -> TickerSnapshot:
        return TickerSnapshot(
            symbol=symbol,
            last_price=Decimal("150"),
            mark_price=Decimal("150"),
            index_price=Decimal("150"),
            bid_price=Decimal("149.99"),
            ask_price=Decimal("150.01"),
            bid_size=Decimal("10"),
            ask_size=Decimal("9"),
            volume_24h=Decimal("100000"),
            turnover_24h=Decimal("15000000"),
            open_interest=Decimal("1000"),
            open_interest_value=Decimal("150000"),
            funding_rate=Decimal("0.0001"),
            next_funding_time_ms=NOW_MS + 3_600_000,
        )

    def get_klines(self, symbol: str, interval: str, *, limit: int = 200) -> tuple[Candle, ...]:
        del symbol, limit
        interval_minutes = int(interval)
        step_ms = interval_minutes * 60_000
        count = 140
        last_start = NOW_MS - step_ms
        if self.stale:
            last_start -= step_ms * 10
        first_start = last_start - step_ms * (count - 1)
        pattern = (
            Decimal("0"),
            Decimal("1"),
            Decimal("2"),
            Decimal("1"),
            Decimal("0"),
            Decimal("-1"),
        )
        rows: list[Candle] = []
        for index in range(count):
            trend = Decimal(index) * Decimal("0.35")
            close = Decimal("100") + trend + pattern[index % 6]
            open_price = close - Decimal("0.15")
            high = max(open_price, close) + Decimal("0.35")
            low = min(open_price, close) - Decimal("0.35")
            rows.append(
                Candle(
                    start_time_ms=first_start + index * step_ms,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=Decimal("100") + Decimal(index),
                    turnover=(Decimal("100") + Decimal(index)) * close,
                )
            )
        return tuple(rows)

    def get_open_interest(
        self,
        symbol: str,
        *,
        interval_time: str = "5min",
        limit: int = 50,
    ) -> tuple[OpenInterestPoint, ...]:
        del symbol, interval_time, limit
        return (
            OpenInterestPoint(timestamp_ms=NOW_MS - 300_000, open_interest=Decimal("1000")),
            OpenInterestPoint(timestamp_ms=NOW_MS, open_interest=Decimal("1010")),
        )


def test_pipeline_returns_ranked_result_for_fresh_symbol() -> None:
    pipeline = DiscoveryPipeline(
        FakePublicClient(),  # type: ignore[arg-type]
        universe=("BTCUSDT",),
        time_source_ms=lambda: NOW_MS,
    )
    run = pipeline.run()
    assert run.healthy_symbol_count == 1
    assert not run.failures
    assert run.results[0].symbol == "BTCUSDT"


def test_stale_symbol_is_quarantined_without_fake_candidate() -> None:
    pipeline = DiscoveryPipeline(
        FakePublicClient(stale=True),  # type: ignore[arg-type]
        universe=("BTCUSDT",),
        time_source_ms=lambda: NOW_MS,
    )
    run = pipeline.run()
    assert not run.results
    assert len(run.failures) == 1
    assert run.failures[0].symbol == "BTCUSDT"
    assert "stale" in run.failures[0].reason.lower()


def test_open_interest_change_uses_two_exact_points() -> None:
    points = (
        OpenInterestPoint(timestamp_ms=1, open_interest=Decimal("100")),
        OpenInterestPoint(timestamp_ms=2, open_interest=Decimal("110")),
    )
    assert DiscoveryPipeline._open_interest_change(points) == Decimal("0.1")
