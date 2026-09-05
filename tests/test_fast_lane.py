from __future__ import annotations

from decimal import Decimal

from crypto_scanner.bybit.models import Candle, InstrumentInfo, TickerSnapshot
from crypto_scanner.discovery import DiscoveryResult, DiscoveryStatus, TradeDirection
from crypto_scanner.fast_lane import (
    FastLaneEvidence,
    ReadinessStatus,
    evaluate_execution_readiness,
)
from crypto_scanner.signal_geometry import EntryMode, SignalGeometry


def _candles(interval_minutes: int, now_ms: int) -> tuple[Candle, ...]:
    interval_ms = interval_minutes * 60_000
    start = now_ms - 100 * interval_ms
    return tuple(
        Candle(
            start_time_ms=start + index * interval_ms,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
            turnover=Decimal("1000"),
        )
        for index in range(100)
    )


def _candidate() -> DiscoveryResult:
    return DiscoveryResult(
        symbol="BTCUSDT",
        direction=TradeDirection.LONG,
        status=DiscoveryStatus.CANDIDATE,
        base_long_score=Decimal("75"),
        base_short_score=Decimal("20"),
        long_score=Decimal("75"),
        short_score=Decimal("20"),
        evidence_coverage=Decimal("0.9"),
        frames=(),
        reasons=("DISCOVERY_EVIDENCE_ALIGNED",),
    )


def _ticker() -> TickerSnapshot:
    return TickerSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("100.05"),
        mark_price=Decimal("100.04"),
        index_price=Decimal("100.03"),
        bid_price=Decimal("100.00"),
        ask_price=Decimal("100.02"),
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
        volume_24h=Decimal("100000"),
        turnover_24h=Decimal("10000000"),
        open_interest=Decimal("1000"),
        open_interest_value=Decimal("100000"),
        funding_rate=Decimal("0.0001"),
        next_funding_time_ms=None,
    )


def _instrument() -> InstrumentInfo:
    return InstrumentInfo(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.01"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_order_qty=Decimal("100"),
        max_market_order_qty=Decimal("50"),
        min_leverage=Decimal("1"),
        max_leverage=Decimal("100"),
        leverage_step=Decimal("0.01"),
    )


def _geometry() -> SignalGeometry:
    return SignalGeometry(
        symbol="BTCUSDT",
        direction=TradeDirection.LONG,
        entry_mode=EntryMode.HL_PULLBACK,
        entry_price=Decimal("100.02"),
        stop_loss=Decimal("99"),
        take_profit_1=Decimal("102"),
        take_profit_2=Decimal("103"),
        initial_risk=Decimal("1.02"),
        rr_tp1=Decimal("1.94"),
        rr_tp2=Decimal("2.92"),
        reference_swing=Decimal("99.2"),
        breakout_level=None,
        atr_3m=Decimal("0.5"),
        chase_atr=Decimal("0"),
    )


def test_stale_quote_rejects_before_geometry(monkeypatch) -> None:
    import crypto_scanner.fast_lane as module

    now_ms = 1_800_000_000_000
    called = False

    def fake_geometry(*_args, **_kwargs):
        nonlocal called
        called = True
        return _geometry()

    monkeypatch.setattr(module, "build_signal_geometry", fake_geometry)
    decision = evaluate_execution_readiness(
        _candidate(),
        candles_3m=_candles(3, now_ms),
        candles_5m=_candles(5, now_ms),
        ticker=_ticker(),
        instrument=_instrument(),
        evidence=FastLaneEvidence(
            quote_timestamp_ms=now_ms - 5_000,
            candidate_timestamp_ms=now_ms - 60_000,
            orderbook_timestamp_ms=now_ms - 500,
            orderbook_imbalance=Decimal("0.10"),
            taker_pressure=Decimal("0.08"),
        ),
        now_ms=now_ms,
    )

    assert decision.status is ReadinessStatus.REJECTED
    assert "STALE_QUOTE" in decision.reasons
    assert not called


def test_missing_microstructure_fails_closed() -> None:
    now_ms = 1_800_000_000_000
    decision = evaluate_execution_readiness(
        _candidate(),
        candles_3m=_candles(3, now_ms),
        candles_5m=_candles(5, now_ms),
        ticker=_ticker(),
        instrument=_instrument(),
        evidence=FastLaneEvidence(
            quote_timestamp_ms=now_ms - 500,
            candidate_timestamp_ms=now_ms - 60_000,
            orderbook_timestamp_ms=now_ms - 500,
            orderbook_imbalance=None,
            taker_pressure=None,
        ),
        now_ms=now_ms,
    )

    assert decision.status is ReadinessStatus.REJECTED
    assert "ORDERBOOK_IMBALANCE_MISSING" in decision.reasons
    assert "TAKER_PRESSURE_MISSING" in decision.reasons


def test_all_hard_guards_can_produce_execution_ready(monkeypatch) -> None:
    import crypto_scanner.fast_lane as module

    now_ms = 1_800_000_000_000
    monkeypatch.setattr(module, "build_signal_geometry", lambda *_args, **_kwargs: _geometry())
    decision = evaluate_execution_readiness(
        _candidate(),
        candles_3m=_candles(3, now_ms),
        candles_5m=_candles(5, now_ms),
        ticker=_ticker(),
        instrument=_instrument(),
        evidence=FastLaneEvidence(
            quote_timestamp_ms=now_ms - 500,
            candidate_timestamp_ms=now_ms - 60_000,
            orderbook_timestamp_ms=now_ms - 500,
            orderbook_imbalance=Decimal("0.10"),
            taker_pressure=Decimal("0.08"),
        ),
        now_ms=now_ms,
    )

    assert decision.status is ReadinessStatus.EXECUTION_READY
    assert decision.execution_ready
    assert decision.geometry is not None
    assert decision.reasons == ("ALL_HARD_GUARDS_PASSED",)
