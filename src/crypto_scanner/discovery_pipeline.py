from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from crypto_scanner.binance.models import Candle, OpenInterestPoint
from crypto_scanner.binance.public_rest import BinanceDemoPublicRestClient, BinancePublicApiError
from crypto_scanner.config import DEFAULT_UNIVERSE
from crypto_scanner.discovery import (
    CryptoNativeEvidence,
    DiscoveryResult,
    analyze_symbol,
    apply_market_context,
)
from crypto_scanner.technical import TechnicalDataError, closed_candles

_DISCOVERY_INTERVALS = {"5": 5, "15": 15, "60": 60}


@dataclass(frozen=True, slots=True)
class MicrostructureSnapshot:
    orderbook_imbalance: Decimal | None = None
    taker_pressure: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SymbolScanFailure:
    symbol: str
    reason: str


@dataclass(frozen=True, slots=True)
class DiscoveryRun:
    started_at_ms: int
    completed_at_ms: int
    results: tuple[DiscoveryResult, ...]
    failures: tuple[SymbolScanFailure, ...]

    @property
    def healthy_symbol_count(self) -> int:
        return len(self.results)


class DiscoveryPipeline:
    def __init__(
        self,
        public_client: BinanceDemoPublicRestClient,
        *,
        universe: tuple[str, ...] = DEFAULT_UNIVERSE,
        time_source_ms: Callable[[], int] | None = None,
    ) -> None:
        if not universe:
            raise ValueError("discovery universe cannot be empty")
        if len(set(universe)) != len(universe):
            raise ValueError("discovery universe contains duplicate symbols")
        self._public_client = public_client
        self._universe = tuple(symbol.upper() for symbol in universe)
        self._time_source_ms = time_source_ms or (lambda: time.time_ns() // 1_000_000)

    def run(
        self,
        microstructure: Mapping[str, MicrostructureSnapshot] | None = None,
    ) -> DiscoveryRun:
        started_at_ms = self._time_source_ms()
        microstructure = microstructure or {}
        results: list[DiscoveryResult] = []
        failures: list[SymbolScanFailure] = []

        for symbol in self._universe:
            try:
                result = self._analyze_one(
                    symbol,
                    microstructure.get(symbol, MicrostructureSnapshot()),
                    now_ms=started_at_ms,
                )
            except (BinancePublicApiError, TechnicalDataError, ValueError) as exc:
                failures.append(
                    SymbolScanFailure(symbol=symbol, reason=f"{type(exc).__name__}: {exc}")
                )
                continue
            results.append(result)

        ranked = apply_market_context(tuple(results)) if results else ()
        return DiscoveryRun(
            started_at_ms=started_at_ms,
            completed_at_ms=self._time_source_ms(),
            results=ranked,
            failures=tuple(failures),
        )

    def _analyze_one(
        self,
        symbol: str,
        microstructure: MicrostructureSnapshot,
        *,
        now_ms: int,
    ) -> DiscoveryResult:
        ticker = self._public_client.get_ticker(symbol)
        candle_sets: dict[str, tuple[Candle, ...]] = {}
        for interval, interval_minutes in _DISCOVERY_INTERVALS.items():
            raw = self._public_client.get_klines(symbol, interval, limit=220)
            closed = closed_candles(raw, interval_minutes=interval_minutes, now_ms=now_ms)
            self._assert_fresh_closed_candle(
                symbol,
                interval,
                closed,
                interval_minutes=interval_minutes,
                now_ms=now_ms,
            )
            candle_sets[interval] = closed

        oi_points = self._public_client.get_open_interest(
            symbol,
            interval_time="5min",
            limit=2,
        )
        oi_change = self._open_interest_change(oi_points)
        native = CryptoNativeEvidence(
            spread_bps=ticker.spread_bps,
            funding_rate=ticker.funding_rate,
            open_interest_change=oi_change,
            orderbook_imbalance=microstructure.orderbook_imbalance,
            taker_pressure=microstructure.taker_pressure,
        )
        return analyze_symbol(symbol, candle_sets, native)

    @staticmethod
    def _assert_fresh_closed_candle(
        symbol: str,
        interval: str,
        candles: tuple[Candle, ...],
        *,
        interval_minutes: int,
        now_ms: int,
    ) -> None:
        if len(candles) < 100:
            raise TechnicalDataError(f"{symbol} {interval}m has only {len(candles)} closed candles")
        interval_ms = interval_minutes * 60_000
        last_close_ms = candles[-1].start_time_ms + interval_ms
        if now_ms - last_close_ms > interval_ms * 2:
            raise TechnicalDataError(f"{symbol} {interval}m latest closed candle is stale")
        if last_close_ms > now_ms:
            raise TechnicalDataError(f"{symbol} {interval}m includes an unfinished candle")

    @staticmethod
    def _open_interest_change(points: tuple[OpenInterestPoint, ...]) -> Decimal | None:
        if len(points) < 2:
            return None
        previous = points[-2].open_interest
        current = points[-1].open_interest
        if previous <= 0 or current < 0:
            return None
        return current / previous - Decimal(1)
