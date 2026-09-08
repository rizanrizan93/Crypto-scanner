from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient
from crypto_scanner.binance.private_write import BinanceTestnetOrderClient
from crypto_scanner.binance.public_rest import BinanceDemoPublicRestClient
from crypto_scanner.config import load_runtime_config
from crypto_scanner.execution_plan import TestnetExecutionArm
from crypto_scanner.persistence import SupabasePersistenceConfig
from crypto_scanner.profit_lock import ProfitLockDecision, ProfitLockStatus, run_profit_lock
from crypto_scanner.safety import SafetyContract
from crypto_scanner.strategy_params import load_strategy_parameters
from crypto_scanner.trade_linkage import DurableTradeLinkage


class ProfitLockWatchError(RuntimeError):
    """Raised when a serialized Demo profit-lock watch tick cannot run safely."""


@dataclass(frozen=True, slots=True)
class ProfitLockWatchResult:
    status: str
    venue: str
    environment: str
    live_trading_locked: bool
    decision_count: int
    ratcheted_symbols: tuple[str, ...]
    decisions: tuple[ProfitLockDecision, ...]


def run_profit_lock_tick() -> ProfitLockWatchResult:
    """Evaluate and, when justified, ratchet Demo protectors once.

    This is deliberately a one-shot operation. Cadence is owned by the existing
    serialized scanner runtime so it never creates a second concurrent exchange writer.
    """
    safety = SafetyContract()
    safety.validate()
    arm = TestnetExecutionArm.from_environment()
    arm.require_enabled()
    config = load_runtime_config()
    credentials = BinanceDemoCredentials.from_environment()
    persistence_config = SupabasePersistenceConfig.from_environment()
    if not persistence_config.enabled:
        raise ProfitLockWatchError(
            "profit-lock watch requires dedicated Crypto Scanner Supabase"
        )
    strategy = load_strategy_parameters(persistence_config)

    with (
        BinanceDemoPrivateReadOnlyClient(
            credentials,
            base_url=config.binance_rest_url,
        ) as reader,
        BinanceDemoPublicRestClient(base_url=config.binance_rest_url) as public,
        BinanceTestnetOrderClient(
            credentials,
            arm,
            base_url=config.binance_rest_url,
        ) as writer,
        DurableTradeLinkage(persistence_config) as linkage,
    ):
        decisions = run_profit_lock(
            reader,
            public,
            writer,
            linkage,
            strategy,
        )

    ratcheted = tuple(
        decision.symbol
        for decision in decisions
        if decision.status is ProfitLockStatus.RATCHETED
    )
    return ProfitLockWatchResult(
        status="PASS_PROFIT_LOCK_WATCH",
        venue="BINANCE",
        environment="DEMO",
        live_trading_locked=safety.live_trading_locked,
        decision_count=len(decisions),
        ratcheted_symbols=ratcheted,
        decisions=decisions,
    )


def emit_profit_lock_tick(result: ProfitLockWatchResult) -> None:
    print(
        json.dumps(
            {"profit_lock_watch": asdict(result)},
            sort_keys=True,
            default=str,
        )
    )


def main() -> None:
    emit_profit_lock_tick(run_profit_lock_tick())


if __name__ == "__main__":
    main()
