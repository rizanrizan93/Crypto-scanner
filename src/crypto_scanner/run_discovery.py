from __future__ import annotations

import json

from crypto_scanner.binance.public_rest import BinanceDemoPublicRestClient
from crypto_scanner.config import load_runtime_config
from crypto_scanner.discovery_pipeline import DiscoveryPipeline


def main() -> None:
    config = load_runtime_config()
    with BinanceDemoPublicRestClient(base_url=config.binance_rest_url) as client:
        run = DiscoveryPipeline(client, universe=config.universe).run()

    payload = {
        "venue": "BINANCE",
        "environment": "DEMO",
        "started_at_ms": run.started_at_ms,
        "completed_at_ms": run.completed_at_ms,
        "healthy_symbol_count": run.healthy_symbol_count,
        "results": [
            {
                "symbol": result.symbol,
                "status": result.status.value,
                "direction": result.direction.value,
                "ranking_score": str(result.ranking_score),
                "long_score": str(result.long_score),
                "short_score": str(result.short_score),
                "evidence_coverage": str(result.evidence_coverage),
                "market_context": result.context_bias.value,
                "reasons": list(result.reasons),
                "frames": [
                    {
                        "timeframe": frame.timeframe,
                        "structure": frame.structure.bias.value,
                        "structure_event": frame.structure.event.value,
                        "regime": frame.regime.regime.value,
                        "rsi14": str(frame.rsi14),
                        "adx14": str(frame.regime.adx14),
                        "atr_pct": str(frame.regime.atr_pct),
                        "momentum10": str(frame.regime.momentum10),
                    }
                    for frame in result.frames
                ],
            }
            for result in run.results
        ],
        "failures": [
            {"symbol": failure.symbol, "reason": failure.reason} for failure in run.failures
        ],
        "execution_ready": False,
        "orders_enabled": False,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
