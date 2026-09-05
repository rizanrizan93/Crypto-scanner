from __future__ import annotations

from decimal import Decimal

from crypto_scanner.bybit.models import Candle, InstrumentInfo, TickerSnapshot
from crypto_scanner.discovery import DiscoveryResult, DiscoveryStatus, TradeDirection
from crypto_scanner.signal_geometry import EntryMode, GeometryError, build_signal_geometry
from crypto_scanner.structure import StructuralBias, StructureEvent, StructureState, SwingPoint


def _candles(count: int = 100) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            start_time_ms=index * 180_000,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
            turnover=Decimal("1000"),
        )
        for index in range(count)
    )


def _candidate(status: DiscoveryStatus = DiscoveryStatus.CANDIDATE) -> DiscoveryResult:
    return DiscoveryResult(
        symbol="BTCUSDT",
        direction=TradeDirection.LONG,
        status=status,
        base_long_score=Decimal("75"),
        base_short_score=Decimal("20"),
        long_score=Decimal("75"),
        short_score=Decimal("20"),
        evidence_coverage=Decimal("0.9"),
        frames=(),
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
        tick_size=Decimal("0.1"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_order_qty=Decimal("100"),
        max_market_order_qty=Decimal("50"),
        min_leverage=Decimal("1"),
        max_leverage=Decimal("100"),
        leverage_step=Decimal("0.01"),
    )


def _ticker() -> TickerSnapshot:
    return TickerSnapshot(
        symbol="BTCUSDT",
        last_price=Decimal("100.05"),
        mark_price=Decimal("100.04"),
        index_price=Decimal("100.03"),
        bid_price=Decimal("100.0"),
        ask_price=Decimal("100.1"),
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
        volume_24h=Decimal("100000"),
        turnover_24h=Decimal("10000000"),
        open_interest=Decimal("1000"),
        open_interest_value=Decimal("100000"),
        funding_rate=Decimal("0.0001"),
        next_funding_time_ms=None,
    )


def _structure(*, low: str, high: str, highs: tuple[str, ...]) -> StructureState:
    recent_highs = tuple(
        SwingPoint(index, index * 1_000, Decimal(price))
        for index, price in enumerate(highs, start=1)
    )
    recent_lows = (
        SwingPoint(1, 1_000, Decimal("96")),
        SwingPoint(2, 2_000, Decimal(low)),
    )
    return StructureState(
        bias=StructuralBias.BULLISH,
        event=StructureEvent.NONE,
        recent_highs=recent_highs,
        recent_lows=recent_lows,
        last_swing_high=Decimal(high),
        last_swing_low=Decimal(low),
    )


def test_long_pullback_uses_structural_liquidity_targets(monkeypatch) -> None:
    import crypto_scanner.signal_geometry as module

    structure5 = _structure(low="98", high="103", highs=("103", "106"))
    structure3 = _structure(low="98", high="103", highs=("103", "106"))
    states = iter((structure5, structure3))
    monkeypatch.setattr(module, "analyze_structure", lambda *_args, **_kwargs: next(states))
    monkeypatch.setattr(module, "atr", lambda *_args, **_kwargs: Decimal("1"))
    monkeypatch.setattr(module, "ema", lambda *_args, **_kwargs: Decimal("101"))

    geometry = build_signal_geometry(
        _candidate(),
        candles_3m=_candles(),
        candles_5m=_candles(),
        ticker=_ticker(),
        instrument=_instrument(),
    )

    assert geometry.entry_mode is EntryMode.HL_PULLBACK
    assert geometry.entry_price == Decimal("100.1")
    assert geometry.stop_loss < geometry.entry_price
    assert geometry.take_profit_1 == Decimal("103")
    assert geometry.take_profit_2 == Decimal("106")
    assert geometry.rr_tp1 >= Decimal("1.20")
    assert geometry.rr_tp2 >= Decimal("2.00")


def test_non_candidate_cannot_build_geometry() -> None:
    try:
        build_signal_geometry(
            _candidate(DiscoveryStatus.WATCH),
            candles_3m=_candles(),
            candles_5m=_candles(),
            ticker=_ticker(),
            instrument=_instrument(),
        )
    except GeometryError as exc:
        assert "not a CANDIDATE" in str(exc)
    else:
        raise AssertionError("WATCH result unexpectedly produced trade geometry")
