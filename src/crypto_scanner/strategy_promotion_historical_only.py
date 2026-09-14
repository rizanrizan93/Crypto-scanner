from __future__ import annotations

import json

from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig
from crypto_scanner.strategy_promotion import PromotionStage, read_promotion_state
from crypto_scanner.strategy_promotion_runtime import run_strategy_promotion


def run_legacy_historical_gate_only() -> dict[str, object]:
    config = SupabasePersistenceConfig.from_environment()
    if not config.enabled:
        raise PersistenceError("strategy promotion requires dedicated Crypto Scanner Supabase")
    state = read_promotion_state(config)
    if state is None or state.stage is PromotionStage.HISTORICAL_PENDING:
        return run_strategy_promotion()
    return {
        "status": "PASS_LEGACY_FORWARD_GATE_DELEGATED_TO_MULTI_STRATEGY_POOL",
        "stage": state.stage.value,
        "strategy_id": state.candidate.strategy_id if state.candidate else None,
        "live_trading_locked": True,
        "real_money_trading_enabled": False,
    }


def main() -> None:
    print(
        json.dumps(
            run_legacy_historical_gate_only(),
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
