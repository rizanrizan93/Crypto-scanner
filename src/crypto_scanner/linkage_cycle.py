from __future__ import annotations

import json
import time

from crypto_scanner.binance.microstructure import (
    BinanceDemoMicrostructureClient,
    BinanceMicrostructureError,
)
from crypto_scanner.binance.public_rest import BinanceDemoPublicRestClient
from crypto_scanner.config import load_runtime_config
from crypto_scanner.discovery import DiscoveryStatus
from crypto_scanner.discovery_pipeline import DiscoveryPipeline, MicrostructureSnapshot
from crypto_scanner.execution_plan import TestnetExecutionArm
from crypto_scanner.fast_lane import FastLaneEvidence, evaluate_execution_readiness
from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig
from crypto_scanner.safety import SafetyContract
from crypto_scanner.trade_linkage import DurableTradeLinkage


class LinkageCycleError(RuntimeError):
    """Raised when the read-only durable linkage cycle cannot run safely."""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def main() -> None:
    safety = SafetyContract()
    safety.validate()
    arm = TestnetExecutionArm.from_environment()
    if arm.enabled:
        raise LinkageCycleError("durable linkage proof refuses to run while execution is armed")

    persistence_config = SupabasePersistenceConfig.from_environment()
    if not persistence_config.enabled:
        raise LinkageCycleError("durable linkage cycle requires dedicated Crypto Scanner Supabase")
    config = load_runtime_config()
    micro_failures: list[dict[str, str]] = []

    with (
        BinanceDemoPublicRestClient(base_url=config.binance_rest_url) as public,
        BinanceDemoMicrostructureClient(base_url=config.binance_rest_url) as micro,
        DurableTradeLinkage(persistence_config) as linkage,
    ):
        discovery_micro: dict[str, MicrostructureSnapshot] = {}
        for symbol in config.universe:
            try:
                evidence = micro.get_evidence(symbol)
                discovery_micro[symbol] = MicrostructureSnapshot(
                    orderbook_imbalance=evidence.orderbook_imbalance,
                    taker_pressure=evidence.taker_pressure,
                )
            except (BinanceMicrostructureError, ValueError, RuntimeError) as exc:
                micro_failures.append({"symbol": symbol, "detail": str(exc)})

        run = DiscoveryPipeline(public, universe=config.universe).run(discovery_micro)
        run_id = linkage.save_discovery_run(run)
        readiness_rows: list[dict[str, object]] = []
        durable_signal_ids: list[str] = []

        for candidate in run.results:
            if candidate.status is not DiscoveryStatus.CANDIDATE:
                continue
            try:
                fresh = micro.get_evidence(candidate.symbol)
                ticker = public.get_ticker(candidate.symbol)
                quote_timestamp_ms = _now_ms()
                instrument = public.get_instrument(candidate.symbol)
                candles_3m = public.get_klines(candidate.symbol, "3", limit=200)
                candles_5m = public.get_klines(candidate.symbol, "5", limit=200)
                now_ms = _now_ms()
                decision = evaluate_execution_readiness(
                    candidate,
                    candles_3m=candles_3m,
                    candles_5m=candles_5m,
                    ticker=ticker,
                    instrument=instrument,
                    evidence=FastLaneEvidence(
                        quote_timestamp_ms=quote_timestamp_ms,
                        candidate_timestamp_ms=run.completed_at_ms,
                        orderbook_timestamp_ms=min(fresh.observed_at_ms, now_ms),
                        orderbook_imbalance=fresh.orderbook_imbalance,
                        taker_pressure=fresh.taker_pressure,
                        exchange_healthy=True,
                        orderbook_healthy=True,
                    ),
                    now_ms=now_ms,
                )
                signal_id: str | None = None
                if decision.execution_ready:
                    signal_id = linkage.save_execution_ready_signal(
                        run_id=run_id,
                        candidate=candidate,
                        readiness=decision,
                        candidate_timestamp_ms=run.completed_at_ms,
                        geometry_created_at_ms=now_ms,
                    )
                    durable_signal_ids.append(signal_id)
                readiness_rows.append(
                    {
                        "symbol": candidate.symbol,
                        "status": decision.status.value,
                        "signal_id": signal_id,
                        "reasons": list(decision.reasons),
                        "orderbook_imbalance": str(fresh.orderbook_imbalance),
                        "taker_pressure": str(fresh.taker_pressure),
                    }
                )
            except (PersistenceError, BinanceMicrostructureError, ValueError, RuntimeError) as exc:
                readiness_rows.append(
                    {
                        "symbol": candidate.symbol,
                        "status": "ERROR",
                        "signal_id": None,
                        "reasons": [str(exc)],
                    }
                )

    payload = {
        "status": "PASS_DURABLE_LINKAGE_READONLY",
        "venue": "BINANCE",
        "environment": "DEMO",
        "execution_armed": False,
        "live_trading_locked": safety.live_trading_locked,
        "run_id": run_id,
        "discovery_result_count": len(run.results),
        "candidate_count": sum(
            result.status is DiscoveryStatus.CANDIDATE for result in run.results
        ),
        "durable_signal_count": len(durable_signal_ids),
        "durable_signal_ids": durable_signal_ids,
        "readiness": readiness_rows,
        "microstructure_failures": micro_failures,
        "orders_submitted": 0,
        "calibration_eligible_created": len(durable_signal_ids) > 0,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
