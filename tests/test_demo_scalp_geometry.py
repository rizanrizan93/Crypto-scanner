from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from crypto_scanner.bybit.models import Candle, InstrumentInfo, TickerSnapshot
from crypto_scanner.discovery import DiscoveryResult, DiscoveryStatus, TradeDirection
from crypto_scanner.fast_lane import (
    FastLaneEvidence,
    ReadinessStatus,
    evaluate_execution_readiness,
)
from crypto_scanner.signal_geometry import (
    EntryMode,
    GeometryError,
    build_demo_technical_scalp_geometry,
)
from crypto_scanner.structure import StructuralBias


def _trend_candles(
    *,
    interval_minutes: int,
    now_ms: int,
    direction: TradeDirection,
    count: int = 120,
) -> tuple[Candle, ...]:
    interval_ms = interval_minutes * 60_000
    start = now_ms - count * interval_ms
    candles: list[Candle] = []
    for index in range(count):
        if direction is TradeDirection.SHORT:
            close = Decimal("101") - Decimal(index) * Decimal("0.02")
            open_price = close + Decimal("0.01")
        else:
            close = Decimal("99") + Decimal(index) * Decimal("0.02")
            open_price = close - Decimal("0.01")
        candles.append(
            Candle(
                start_time_ms=start + index * interval_ms,
                open=open_price,
                high=max(open_price, close) + Decimal("0.45"),
                low=min(open_price, close) - Decimal("0.45"),
                close=close,
                volume=Decimal("10"),
                turnover=Decimal("1000"),
            )
        )
    return tuple(candles)


def _candidate(direction: TradeDirection = TradeDirection.SHORT) -> DiscoveryResult:
    bullish = direction is TradeDirection.LONG
    frame_15m = SimpleNamespace(
        timeframe="15",
        last_price=Decimal("102" if bullish else "98"),
        structure=SimpleNamespace(
            bias=StructuralBias.BULLISH if bullish else StructuralBias.BEARISH
        ),
        regime=SimpleNamespace(
            ema20=Decimal("101" if bullish else "99"),
            ema50=Decimal("100"),
            momentum10=Decimal("0.02" if bullish else "-0.02"),
        ),
    )
    return DiscoveryResult(
        symbol="BTCUSDT",
        direction=direction,
        status=DiscoveryStatus.CANDIDATE,
        base_long_score=Decimal("75" if bullish else "20"),
        base_short_score=Decimal("20" if bullish else "75"),
        long_score=Decimal("75" if bullish else "20"),
        short_score=Decimal("20" if bullish else "75"),
        evidence_coverage=Decimal("0.90"),
        frames=(frame_15m,),
        reasons=("DISCOVERY_EVIDENCE_ALIGNED",),
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


def _ticker(price: Decimal) -> TickerSnapshot:
    return TickerSnapshot(
        symbol="BTCUSDT",
        last_price=price,
        mark_price=price,
        index_price=price,
        bid_price=price - Decimal("0.01"),
        ask_price=price + Decimal("0.01"),
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
        volume_24h=Decimal("100000"),
        turnover_24h=Decimal("10000000"),
        open_interest=Decimal("1000"),
        open_interest_value=Decimal("100000"),
        funding_rate=Decimal("0.0001"),
        next_funding_time_ms=None,
    )


def test_demo_short_scalp_geometry_keeps_rr_and_stop_bounds() -> None:
    now_ms = 1_800_000_000_000
    candles_3m = _trend_candles(
        interval_minutes=3,
        now_ms=now_ms,
        direction=TradeDirection.SHORT,
    )
    candles_5m = _trend_candles(
        interval_minutes=5,
        now_ms=now_ms,
        direction=TradeDirection.SHORT,
    )
    ticker = _ticker(candles_3m[-1].close)

    geometry = build_demo_technical_scalp_geometry(
        _candidate(),
        candles_3m=candles_3m,
        candles_5m=candles_5m,
        ticker=ticker,
        instrument=_instrument(),
    )

    assert geometry.entry_mode is EntryMode.TECHNICAL_SCALP
    assert geometry.stop_loss > geometry.entry_price > geometry.take_profit_1
    assert geometry.take_profit_1 > geometry.take_profit_2
    assert geometry.rr_tp1 >= Decimal("1.20")
    assert geometry.rr_tp2 >= Decimal("2.00")
    assert geometry.chase_atr <= Decimal("0.80")
    assert geometry.initial_risk / geometry.atr_3m <= Decimal("1.50")


def test_demo_scalp_geometry_rejects_countertrend_current_confirmation() -> None:
    now_ms = 1_800_000_000_000
    candles_3m = _trend_candles(
        interval_minutes=3,
        now_ms=now_ms,
        direction=TradeDirection.LONG,
    )
    candles_5m = _trend_candles(
        interval_minutes=5,
        now_ms=now_ms,
        direction=TradeDirection.LONG,
    )
    ticker = _ticker(candles_3m[-1].close)

    try:
        build_demo_technical_scalp_geometry(
            _candidate(TradeDirection.SHORT),
            candles_3m=candles_3m,
            candles_5m=candles_5m,
            ticker=ticker,
            instrument=_instrument(),
        )
    except GeometryError as exc:
        assert "lacks current 3m/5m confirmation" in str(exc)
    else:
        raise AssertionError("countertrend Demo fallback unexpectedly produced geometry")


def test_demo_readiness_uses_scalp_fallback_after_strict_geometry_failure(
    monkeypatch,
) -> None:
    import crypto_scanner.fast_lane as module

    now_ms = 1_800_000_000_000
    candles_3m = _trend_candles(
        interval_minutes=3,
        now_ms=now_ms,
        direction=TradeDirection.SHORT,
    )
    candles_5m = _trend_candles(
        interval_minutes=5,
        now_ms=now_ms,
        direction=TradeDirection.SHORT,
    )
    ticker = _ticker(candles_3m[-1].close)
    monkeypatch.setenv("CRYPTO_SCANNER_TESTNET_EXECUTION", "ENABLED")
    monkeypatch.setattr(
        module,
        "build_signal_geometry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            GeometryError("insufficient confirmed swing highs/lows")
        ),
    )

    decision = evaluate_execution_readiness(
        _candidate(),
        candles_3m=candles_3m,
        candles_5m=candles_5m,
        ticker=ticker,
        instrument=_instrument(),
        evidence=FastLaneEvidence(
            quote_timestamp_ms=now_ms - 100,
            candidate_timestamp_ms=now_ms - 60_000,
            orderbook_timestamp_ms=now_ms - 100,
            orderbook_imbalance=Decimal("-0.10"),
            taker_pressure=Decimal("-0.10"),
        ),
        now_ms=now_ms,
    )

    assert decision.status is ReadinessStatus.EXECUTION_READY
    assert decision.geometry is not None
    assert decision.geometry.entry_mode is EntryMode.TECHNICAL_SCALP
    assert "DEMO_TECHNICAL_15M_SCALP_GEOMETRY" in decision.reasons


def test_non_demo_does_not_use_scalp_geometry_fallback(monkeypatch) -> None:
    import crypto_scanner.fast_lane as module

    now_ms = 1_800_000_000_000
    candles_3m = _trend_candles(
        interval_minutes=3,
        now_ms=now_ms,
        direction=TradeDirection.SHORT,
    )
    candles_5m = _trend_candles(
        interval_minutes=5,
        now_ms=now_ms,
        direction=TradeDirection.SHORT,
    )
    ticker = _ticker(candles_3m[-1].close)
    monkeypatch.delenv("CRYPTO_SCANNER_TESTNET_EXECUTION", raising=False)
    monkeypatch.setattr(
        module,
        "build_signal_geometry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            GeometryError("insufficient confirmed swing highs/lows")
        ),
    )
    monkeypatch.setattr(
        module,
        "build_demo_technical_scalp_geometry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Demo fallback must not be called when execution is disabled")
        ),
    )

    decision = evaluate_execution_readiness(
        _candidate(),
        candles_3m=candles_3m,
        candles_5m=candles_5m,
        ticker=ticker,
        instrument=_instrument(),
        evidence=FastLaneEvidence(
            quote_timestamp_ms=now_ms - 100,
            candidate_timestamp_ms=now_ms - 60_000,
            orderbook_timestamp_ms=now_ms - 100,
            orderbook_imbalance=Decimal("-0.10"),
            taker_pressure=Decimal("-0.10"),
        ),
        now_ms=now_ms,
    )

    assert decision.status is ReadinessStatus.REJECTED
    assert any(reason.startswith("GEOMETRY_INVALID:") for reason in decision.reasons)
