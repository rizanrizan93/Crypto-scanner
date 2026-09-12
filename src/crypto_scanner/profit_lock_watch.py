from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, replace

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient
from crypto_scanner.binance.private_write import BinanceTestnetOrderClient
from crypto_scanner.binance.public_rest import BinanceDemoPublicRestClient
from crypto_scanner.config import load_runtime_config
from crypto_scanner.execution_plan import TestnetExecutionArm
from crypto_scanner.lifecycle_maintenance import run_lifecycle_maintenance
from crypto_scanner.management_health import record_tick
from crypto_scanner.persistence import SupabasePersistenceConfig, read_retry_count
from crypto_scanner.profit_lock import ProfitLockDecision, ProfitLockStatus, run_profit_lock
from crypto_scanner.safety import SafetyContract
from crypto_scanner.trade_linkage import DurableTradeLinkage


class ProfitLockWatchError(RuntimeError):
    """Raised when a serialized Demo profit-lock watch tick cannot run safely."""


_ACTIVE_ALGO_STATUSES = frozenset({"NEW", "PENDING", "WORKING"})


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
    tick_duration_seconds: float = 0.0
    orphan_cleanup_status: str = "NOT_REQUIRED"
    cleaned_orphan_symbols: tuple[str, ...] = ()
    cancelled_orphan_ids: tuple[str, ...] = ()
    orphan_cleanup_blockers: tuple[str, ...] = ()


def run_profit_lock_tick() -> ProfitLockWatchResult:
    started = time.monotonic()
    result = _run_profit_lock_tick()
    return replace(result, tick_duration_seconds=round(time.monotonic() - started, 6))


def _scanner_owned_active_algos(reader: BinanceDemoPrivateReadOnlyClient) -> tuple[object, ...]:
    """Read-only precheck so ordinary flat ticks never initialize an exchange writer."""
    return tuple(
        order
        for order in reader.get_open_algo_orders()
        if order.status.upper() in _ACTIVE_ALGO_STATUSES
        and order.client_algo_id.startswith("cs-")
    )


def _clean_flat_orphans(
    reader: BinanceDemoPrivateReadOnlyClient,
    config,
    credentials,
    arm,
    safety: SafetyContract,
) -> ProfitLockWatchResult:
    """Clean scanner-owned flat protectors only when the read-only precheck finds one.

    Lifecycle maintenance re-reads authoritative positions before cancellation. This
    makes the cleanup safe if a position appears between the initial flat read and the
    cancellation attempt. Manual/non-scanner conditional orders remain untouched.
    """
    if not _scanner_owned_active_algos(reader):
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

    with BinanceTestnetOrderClient(
        credentials,
        arm,
        base_url=config.binance_rest_url,
    ) as writer:
        maintenance = run_lifecycle_maintenance(reader, writer, safety=safety)

    # Maintenance performs its own authoritative position re-read before any cancel.
    # If a position appeared during the race window, manage it normally rather than
    # reporting the stale initial flat observation as final state.
    if maintenance.open_position_symbols:
        persistence_config = SupabasePersistenceConfig.from_environment()
        if not persistence_config.enabled:
            raise ProfitLockWatchError(
                "profit-lock watch requires dedicated Crypto Scanner Supabase"
            )
        return _manage_open(
            reader,
            config,
            credentials,
            arm,
            persistence_config,
            len(maintenance.open_position_symbols),
        )

    blocked = bool(maintenance.blockers)
    cleaned = bool(maintenance.cancelled_ids)
    return ProfitLockWatchResult(
        status=(
            "BLOCKED_FLAT_ORPHAN"
            if blocked
            else "PASS_NO_POSITION_ORPHANS_CLEANED"
            if cleaned
            else "PASS_NO_POSITION"
        ),
        venue="BINANCE",
        environment="DEMO",
        live_trading_locked=safety.live_trading_locked,
        decision_count=0,
        ratcheted_symbols=(),
        decisions=(),
        strategy_source="NOT_REQUIRED_FLAT",
        persistence_status="NOT_REQUIRED",
        degraded_reason="FLAT_ORPHAN_CLEANUP_BLOCKED" if blocked else None,
        orphan_cleanup_status=maintenance.status,
        cleaned_orphan_symbols=maintenance.cleaned_symbols,
        cancelled_orphan_ids=maintenance.cancelled_ids,
        orphan_cleanup_blockers=maintenance.blockers,
    )


def _run_profit_lock_tick() -> ProfitLockWatchResult:
    """Evaluate and, when justified, ratchet Demo protectors once.

    This is deliberately a one-shot operation. Cadence is owned by the existing
    serialized scanner runtime so it never creates a second concurrent exchange writer.
    Flat ticks first remain read-only; a writer is initialized only when an active
    scanner-owned conditional order proves orphan cleanup is required.
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
            return _clean_flat_orphans(reader, config, credentials, arm, safety)
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
    degraded = result.status in {"DEGRADED_PERSISTENCE_TRANSIENT", "BLOCKED_FLAT_ORPHAN"}
    health = record_tick(degraded=degraded)
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
