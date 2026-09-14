from __future__ import annotations

import json
import time
from dataclasses import asdict

from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig
from crypto_scanner.regime_specialist_demo import REGIME_SPECIALIST_STRATEGY_ID
from crypto_scanner.strategy_params import load_strategy_parameters
from crypto_scanner.strategy_promotion import (
    PromotionStage,
    StrategyPromotionState,
    StrategyVersion,
    read_promotion_state,
    save_promotion_state,
)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def promote_regime_specialist_to_forward_demo() -> StrategyPromotionState:
    """One-way manual promotion authorized by the user for Binance Futures DEMO only.

    This does not unlock live trading and does not mark the strategy as production.
    It seeds FORWARD_DEMO from the externally completed 2012-2026 regime research.
    Subsequent promotion/rollback remains governed by the normal forward-demo gate.
    """
    config = SupabasePersistenceConfig.from_environment()
    if not config.enabled:
        raise PersistenceError("regime specialist promotion requires Crypto Scanner Supabase")

    current = read_promotion_state(config)
    if current is not None:
        if current.champion and current.champion.strategy_id == REGIME_SPECIALIST_STRATEGY_ID:
            return current
        if current.candidate and current.candidate.strategy_id == REGIME_SPECIALIST_STRATEGY_ID:
            return current
        historical_id = (
            str((current.historical_evidence or {}).get("strategy_id") or "")
        )
        if historical_id == REGIME_SPECIALIST_STRATEGY_ID and current.stage in {
            PromotionStage.ROLLED_BACK,
            PromotionStage.QUARANTINED,
            PromotionStage.PROMOTED,
        }:
            # Never silently re-arm a strategy after the autonomous forward gate has
            # reached a terminal decision.
            return current
        if current.stage in {PromotionStage.FORWARD_DEMO, PromotionStage.QUARANTINED}:
            raise PersistenceError(
                f"cannot replace active promotion state stage={current.stage.value}"
            )

    params = load_strategy_parameters(config)
    timestamp = _now_ms()
    candidate = StrategyVersion(
        strategy_id=REGIME_SPECIALIST_STRATEGY_ID,
        params=params,
        created_at_ms=timestamp,
        source="USER_APPROVED_REGIME_RESEARCH_2012_2026",
    )
    candidate.validate()
    revision = 1 if current is None else current.revision + 1
    state = StrategyPromotionState(
        stage=PromotionStage.FORWARD_DEMO,
        revision=revision,
        champion=None if current is None else current.champion,
        candidate=candidate,
        historical_evidence={
            "strategy_id": REGIME_SPECIALIST_STRATEGY_ID,
            "research_window": "2012-01-01/2026-09-12",
            "executable_multi_asset_window": "2017-01-01/2026-09-12",
            "oos_window": "2025-01-01/2026-09-12",
            "bull_rule": "BTC_D1_ABOVE_EMA200_AND_MOM60_POSITIVE_PLUS_REGIME_SWITCH",
            "bear_rule": "BOTTOM3_28D_WEEKLY_SHORT",
            "bear_research_sleeve_fraction": "0.25",
            "demo_bear_risk_fraction_per_entry": "0.00125",
            "demo_bull_risk_fraction_per_entry": "0.005",
            "oos_cagr_pct": "10.37",
            "oos_max_drawdown_pct": "-5.92",
            "oos_sharpe": "0.91",
            "execution_adapter": "EXISTING_MICROSTRUCTURE_GEOMETRY_SL_TP_PROFIT_LOCK",
            "historical_execution_adapter_equivalence": False,
            "manual_forward_demo_authorized": True,
            "live_trading_locked": True,
        },
        forward_evidence=None,
        updated_at_ms=timestamp,
        reason="USER_APPROVED_FORWARD_DEMO_AFTER_2012_2026_REGIME_RESEARCH",
    )
    save_promotion_state(
        config,
        state,
        expected_revision=None if current is None else current.revision,
    )
    return state


def main() -> None:
    state = promote_regime_specialist_to_forward_demo()
    payload = state.to_dict()
    # Avoid serializing dataclass internals or any credentials; state is non-secret.
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
