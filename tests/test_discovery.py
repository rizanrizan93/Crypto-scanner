from __future__ import annotations

from decimal import Decimal

from crypto_scanner.bybit.models import Candle
from crypto_scanner.discovery import (
    CryptoNativeEvidence,
    DiscoveryStatus,
    MarketContextBias,
    TradeDirection,
    analyze_symbol,
    apply_market_context,
)


def _candles(*, bullish: bool, count: int = 140) -> tuple[Candle, ...]:
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
        center = Decimal("100") + (trend if bullish else -trend)
        wave = pattern[index % 6]
        close = center + (wave if bullish else -wave)
        open_price = close - (Decimal("0.15") if bullish else Decimal("-0.15"))
        high = max(open_price, close) + Decimal("0.35")
        low = min(open_price, close) - Decimal("0.35")
        rows.append(
            Candle(
                start_time_ms=index * 300_000,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=Decimal("100") + Decimal(index),
                turnover=(Decimal("100") + Decimal(index)) * close,
            )
        )
    return tuple(rows)


def _native(*, bullish: bool, spread_bps: str = "1.5") -> CryptoNativeEvidence:
    return CryptoNativeEvidence(
        spread_bps=Decimal(spread_bps),
        funding_rate=Decimal("0.0001") if bullish else Decimal("-0.0001"),
        open_interest_change=Decimal("0.01"),
        orderbook_imbalance=Decimal("0.20") if bullish else Decimal("-0.20"),
        taker_pressure=Decimal("0.25") if bullish else Decimal("-0.25"),
    )


def _analyze(symbol: str, *, bullish: bool, spread_bps: str = "1.5"):
    candles = _candles(bullish=bullish)
    return analyze_symbol(
        symbol,
        {"5": candles, "15": candles, "60": candles},
        _native(bullish=bullish, spread_bps=spread_bps),
    )


def test_bullish_structure_is_long_candidate() -> None:
    result = _analyze("BTCUSDT", bullish=True)
    assert result.direction is TradeDirection.LONG
    assert result.status is DiscoveryStatus.CANDIDATE
    assert result.long_score > result.short_score
    assert result.evidence_coverage == Decimal(1)


def test_bearish_structure_is_short_candidate() -> None:
    result = _analyze("ETHUSDT", bullish=False)
    assert result.direction is TradeDirection.SHORT
    assert result.status is DiscoveryStatus.CANDIDATE
    assert result.short_score > result.long_score


def test_wide_spread_forces_no_trade_even_with_strong_structure() -> None:
    result = _analyze("BTCUSDT", bullish=True, spread_bps="12")
    assert result.status is DiscoveryStatus.NO_TRADE
    assert "SPREAD_TOO_WIDE" in result.reasons


def test_btc_eth_alignment_adjusts_altcoin_context_without_creating_entry() -> None:
    btc = _analyze("BTCUSDT", bullish=True)
    eth = _analyze("ETHUSDT", bullish=True)
    sol = _analyze("SOLUSDT", bullish=True)

    ranked = apply_market_context((sol, eth, btc))
    by_symbol = {result.symbol: result for result in ranked}

    assert by_symbol["SOLUSDT"].context_bias is MarketContextBias.BULLISH
    assert by_symbol["SOLUSDT"].context_adjustment_long == Decimal(4)
    assert by_symbol["SOLUSDT"].long_score >= sol.long_score
    assert not hasattr(by_symbol["SOLUSDT"], "entry")
    assert not hasattr(by_symbol["SOLUSDT"], "execution_ready")


def test_btc_eth_bullish_context_penalizes_altcoin_short_bias() -> None:
    btc = _analyze("BTCUSDT", bullish=True)
    eth = _analyze("ETHUSDT", bullish=True)
    xrp = _analyze("XRPUSDT", bullish=False)

    adjusted = {item.symbol: item for item in apply_market_context((btc, eth, xrp))}
    assert adjusted["XRPUSDT"].context_adjustment_short == Decimal(-8)
    assert adjusted["XRPUSDT"].short_score < xrp.short_score
