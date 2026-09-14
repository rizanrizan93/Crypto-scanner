from __future__ import annotations

import json
import sys
from dataclasses import asdict

from crypto_scanner.funding_oi_cycle import run_funding_oi_cycle
from crypto_scanner.funding_oi_demo import FUNDING_OI_STRATEGY_ID
from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig
from crypto_scanner.regime_specialist_cycle import run_regime_specialist_cycle
from crypto_scanner.regime_specialist_demo import REGIME_SPECIALIST_STRATEGY_ID
from crypto_scanner.strategy_pool import strategy_demo_execution_authorized
from crypto_scanner.volatility_breakout_cycle import run_volatility_breakout_cycle
from crypto_scanner.volatility_breakout_demo import VOLATILITY_BREAKOUT_STRATEGY_ID

_RUNNERS = {
    REGIME_SPECIALIST_STRATEGY_ID: run_regime_specialist_cycle,
    VOLATILITY_BREAKOUT_STRATEGY_ID: run_volatility_breakout_cycle,
    FUNDING_OI_STRATEGY_ID: run_funding_oi_cycle,
}


def run_pool_strategy(strategy_id: str) -> dict[str, object]:
    runner = _RUNNERS.get(strategy_id)
    if runner is None:
        raise PersistenceError(f"unknown Demo strategy id: {strategy_id}")
    config = SupabasePersistenceConfig.from_environment()
    if not config.enabled:
        raise PersistenceError("strategy pool cycle requires dedicated Crypto Scanner Supabase")
    if not strategy_demo_execution_authorized(config, strategy_id):
        return {
            "status": "PASS_DEMO_POOL_DISABLED",
            "strategy_id": strategy_id,
            "execution_authorized": False,
            "live_trading_locked": True,
            "real_money_trading_enabled": False,
        }
    result = runner()
    payload = asdict(result)
    payload["execution_authorized"] = True
    payload["real_money_trading_enabled"] = False
    return payload


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m crypto_scanner.strategy_pool_cycle <strategy-id>")
    payload = run_pool_strategy(sys.argv[1])
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    if payload.get("execution_error") is not None:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
