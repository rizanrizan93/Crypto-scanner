from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient
from crypto_scanner.binance.private_write import BinanceTestnetOrderClient
from crypto_scanner.binance.public_rest import BinanceDemoPublicRestClient
from crypto_scanner.config import load_runtime_config
from crypto_scanner.execution_plan import TestnetExecutionArm
from crypto_scanner.management_health import record_tick
from crypto_scanner.persistence import SupabasePersistenceConfig, read_retry_count
from crypto_scanner.profit_lock import ProfitLockDecision, ProfitLockStatus, run_profit_lock
from crypto_scanner.safety import SafetyContract
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
    open_position_count: int = 0
    strategy_source: str = "ENTRY_SNAPSHOT"
    persistence_status: str = "HEALTHY"
    degraded_reason: str | None = None
    retry_count: int = 0


def run_profit_lock_tick() -> ProfitLockWatchResult:
    """Evaluate and, when justified, ratchet Demo protectors once.

    This is deliberately a one-shot operation. Cadence is owned by the existing
    serialized scanner runtime so it never creates a second concurrent exchange writer.
    """
    read_retry_count.set(0)
    safety = SafetyContract()
    safety.validate()
    arm = TestnetExecutionArm.from_environment()
    arm.require_enabled()
    config = load_runtime_config()
    credentials = BinanceDemoCredentials.from_environment()
    persistence_config = SupabasePersistenceConfig.from_environment()
    if not persistence_config.enabled:
        raise ProfitLockWatchError("profit-lock watch requires dedicated Crypto Scanner Supabase")

    with BinanceDemoPrivateReadOnlyClient(
        credentials,
        base_url=config.binance_rest_url,
    ) as reader:
        positions = tuple(p for p in reader.get_positions() if p.is_open)
        if not positions:
            return ProfitLockWatchResult(
                "PASS_NO_POSITION",
                "BINANCE",
                "DEMO",
                safety.live_trading_locked,
                0,
                (),
                (),
                strategy_source="NOT_REQUIRED_FLAT",
                persistence_status="NOT_REQUIRED",
            )
        return _manage_open(reader, config, credentials, arm, persistence_config, len(positions))


def _manage_open(reader, config, credentials, arm, persistence_config, position_count):
    with (
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
        )

    ratcheted = tuple(
        decision.symbol for decision in decisions if decision.status is ProfitLockStatus.RATCHETED
    )
    degraded = any(d.status is ProfitLockStatus.DEGRADED_PERSISTENCE_TRANSIENT for d in decisions)
    return ProfitLockWatchResult(
        status="DEGRADED_PERSISTENCE_TRANSIENT" if degraded else "PASS_PROFIT_LOCK_WATCH",
        venue="BINANCE",
        environment="DEMO",
        live_trading_locked=True,
        decision_count=len(decisions),
        ratcheted_symbols=ratcheted,
        decisions=decisions,
        open_position_count=position_count,
        persistence_status="TRANSIENT_UNAVAILABLE" if degraded else "HEALTHY",
        degraded_reason="CONTEXT_READ_RETRIES_EXHAUSTED" if degraded else None,
        retry_count=read_retry_count.get(),
    )


def emit_profit_lock_tick(result: ProfitLockWatchResult) -> None:
    health = record_tick(degraded=result.status == "DEGRADED_PERSISTENCE_TRANSIENT")
    print(
        json.dumps(
            {"profit_lock_watch": asdict(result), "management_heartbeat": health},
            sort_keys=True,
            default=str,
        )
    )


def main() -> None:
    emit_profit_lock_tick(run_profit_lock_tick())


if __name__ == "__main__":
    main()
