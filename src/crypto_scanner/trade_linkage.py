from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from decimal import Decimal

from crypto_scanner.binance.models import PositionSnapshot
from crypto_scanner.binance.private_rest import UserTradeFill
from crypto_scanner.discovery import DiscoveryResult, TradeDirection
from crypto_scanner.discovery_pipeline import DiscoveryRun
from crypto_scanner.execution_plan import EntryOrderPlan
from crypto_scanner.fast_lane import ReadinessDecision, ReadinessStatus
from crypto_scanner.persistence import (
    SCHEMA_VERSION,
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseRestClient,
    _without_none,
)


@dataclass(frozen=True, slots=True)
class DurableTradeContext:
    position_id: str
    signal_id: str | None
    initial_stop_loss: Decimal | None
    setup: str | None
    regime: str | None
    calibration_eligible: bool


def stable_run_id(run: DiscoveryRun) -> str:
    digest = hashlib.sha256(
        f"BINANCE|DEMO|DISCOVERY|{run.started_at_ms}".encode()
    ).hexdigest()[:24]
    return f"run-{digest}"


def stable_signal_id(
    run_id: str,
    candidate: DiscoveryResult,
    *,
    candidate_timestamp_ms: int,
) -> str:
    raw = (
        f"{run_id}|{candidate.symbol}|{candidate.direction.value}|{candidate_timestamp_ms}"
    ).encode()
    return f"sig-{hashlib.sha256(raw).hexdigest()[:32]}"


def stable_position_id_from_episode(
    symbol: str,
    direction: TradeDirection | str,
    entry_time_ms: int,
) -> str:
    direction_value = direction.value if isinstance(direction, TradeDirection) else str(direction)
    raw = f"BINANCE|DEMO|{symbol.upper()}|{direction_value}|{entry_time_ms}".encode()
    return f"pos-{hashlib.sha256(raw).hexdigest()[:32]}"


def _regime(candidate: DiscoveryResult) -> str | None:
    for frame in candidate.frames:
        if frame.timeframe == "15":
            return frame.regime.regime.value
    return candidate.frames[0].regime.regime.value if candidate.frames else None


class _LinkageRestClient(SupabaseRestClient):
    def select(
        self,
        table: str,
        *,
        params: dict[str, str],
    ) -> list[dict[str, object]]:
        if not table.replace("_", "").isalnum():
            raise PersistenceError("invalid persistence table name")
        response = self._client.get(
            f"{self.base_url}/rest/v1/{table}",
            params=params,
            headers=self._headers(),
        )
        if response.is_error:
            raise PersistenceError(
                f"Supabase select failed table={table} status={response.status_code}: "
                f"{response.text[:500]}"
            )
        payload = response.json()
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise PersistenceError(f"Supabase select returned invalid payload for {table}")
        return payload

    def patch(
        self,
        table: str,
        *,
        filters: dict[str, str],
        values: dict[str, object],
    ) -> None:
        if not table.replace("_", "").isalnum():
            raise PersistenceError("invalid persistence table name")
        response = self._client.patch(
            f"{self.base_url}/rest/v1/{table}",
            params=filters,
            headers=self._headers(prefer="return=minimal"),
            json=_without_none(values),
        )
        if response.is_error:
            raise PersistenceError(
                f"Supabase patch failed table={table} status={response.status_code}: "
                f"{response.text[:500]}"
            )


class DurableTradeLinkage:
    """Durable identity chain from discovery evidence through the closed trade."""

    def __init__(
        self,
        config: SupabasePersistenceConfig,
        *,
        client=None,
    ) -> None:
        self._rest = _LinkageRestClient(config, client=client)

    def close(self) -> None:
        self._rest.close()

    def __enter__(self) -> DurableTradeLinkage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def assert_schema_current(self) -> None:
        actual = self._rest.schema_version()
        if actual != SCHEMA_VERSION:
            raise PersistenceError(
                f"Supabase schema mismatch expected={SCHEMA_VERSION} actual={actual}"
            )

    def save_discovery_run(
        self,
        run: DiscoveryRun,
        *,
        execution_armed: bool = False,
    ) -> str:
        self.assert_schema_current()
        run_id = stable_run_id(run)
        failures = [
            {"symbol": failure.symbol, "reason": failure.reason} for failure in run.failures
        ]
        status = "COMPLETED" if not failures else "COMPLETED_PARTIAL"
        self._rest.upsert(
            "scanner_runs",
            (
                {
                    "run_id": run_id,
                    "started_at_ms": run.started_at_ms,
                    "completed_at_ms": run.completed_at_ms,
                    "status": status,
                    "source": "DISCOVERY",
                    "git_sha": os.getenv("GITHUB_SHA", "").strip() or None,
                    "execution_armed": execution_armed,
                    "metadata": {
                        "venue": "BINANCE",
                        "environment": "DEMO",
                        "healthy_symbol_count": run.healthy_symbol_count,
                        "failures": failures,
                    },
                },
            ),
            on_conflict=("run_id",),
        )
        rankings = tuple(
            {
                "run_id": run_id,
                "symbol": result.symbol,
                "ranked_at_ms": run.completed_at_ms,
                "rank": rank,
                "score": result.ranking_score,
                "direction": result.direction,
                "status": result.status,
                "regime": _regime(result),
                "evidence_coverage": result.evidence_coverage,
                "reasons": list(result.reasons),
            }
            for rank, result in enumerate(run.results, start=1)
        )
        self._rest.upsert(
            "pair_rankings",
            rankings,
            on_conflict=("run_id", "symbol"),
        )
        return run_id

    def save_execution_ready_signal(
        self,
        *,
        run_id: str,
        candidate: DiscoveryResult,
        readiness: ReadinessDecision,
        candidate_timestamp_ms: int,
        geometry_created_at_ms: int,
    ) -> str:
        if readiness.status is not ReadinessStatus.EXECUTION_READY or readiness.geometry is None:
            raise PersistenceError("only EXECUTION_READY geometry can become a durable signal")
        if candidate.direction not in {TradeDirection.LONG, TradeDirection.SHORT}:
            raise PersistenceError("durable signal direction must be LONG or SHORT")
        geometry = readiness.geometry
        if geometry.symbol != candidate.symbol or geometry.direction is not candidate.direction:
            raise PersistenceError("durable signal geometry does not match discovery candidate")
        signal_id = stable_signal_id(
            run_id,
            candidate,
            candidate_timestamp_ms=candidate_timestamp_ms,
        )
        regime = _regime(candidate)
        if regime is None:
            raise PersistenceError("durable signal requires an explicit market regime")
        self._rest.upsert(
            "signals",
            (
                {
                    "signal_id": signal_id,
                    "run_id": run_id,
                    "symbol": candidate.symbol,
                    "direction": candidate.direction,
                    "setup": geometry.entry_mode,
                    "regime": regime,
                    "status": "EXECUTION_READY",
                    "score": candidate.ranking_score,
                    "created_at_ms": candidate_timestamp_ms,
                    "expires_at_ms": candidate_timestamp_ms + 5 * 60_000,
                    "evidence": {
                        "discovery_reasons": list(candidate.reasons),
                        "readiness_reasons": list(readiness.reasons),
                        "context_bias": candidate.context_bias,
                        "long_score": candidate.long_score,
                        "short_score": candidate.short_score,
                        "evidence_coverage": candidate.evidence_coverage,
                    },
                },
            ),
            on_conflict=("signal_id",),
        )
        self._rest.upsert(
            "signal_geometry",
            (
                {
                    "signal_id": signal_id,
                    "entry_mode": geometry.entry_mode,
                    "entry_price": geometry.entry_price,
                    "stop_loss": geometry.stop_loss,
                    "tp1": geometry.take_profit_1,
                    "tp2": geometry.take_profit_2,
                    "risk_per_unit": geometry.initial_risk,
                    "rr_tp1": geometry.rr_tp1,
                    "rr_tp2": geometry.rr_tp2,
                    "geometry_created_at_ms": geometry_created_at_ms,
                    "raw": {
                        "reference_swing": geometry.reference_swing,
                        "breakout_level": geometry.breakout_level,
                        "atr_3m": geometry.atr_3m,
                        "chase_atr": geometry.chase_atr,
                    },
                },
            ),
            on_conflict=("signal_id",),
        )
        return signal_id

    def save_entry_plan(
        self,
        plan: EntryOrderPlan,
        *,
        status: str,
        venue_order_id: str | None = None,
        avg_price: Decimal | None = None,
        created_at_ms: int | None = None,
        updated_at_ms: int | None = None,
    ) -> None:
        side = "BUY" if plan.side == "Buy" else "SELL" if plan.side == "Sell" else None
        if side is None:
            raise PersistenceError("entry plan side is invalid")
        self._rest.upsert(
            "orders",
            (
                {
                    "client_order_id": plan.order_link_id,
                    "venue_order_id": venue_order_id,
                    "signal_id": plan.signal_id,
                    "symbol": plan.symbol,
                    "side": side,
                    "position_side": "BOTH",
                    "order_type": "MARKET",
                    "status": status,
                    "qty": plan.qty,
                    "price": plan.entry_price,
                    "avg_price": avg_price,
                    "reduce_only": False,
                    "close_position": False,
                    "created_at_ms": created_at_ms,
                    "updated_at_ms": updated_at_ms,
                    "raw": {
                        "risk_fraction": plan.risk_fraction,
                        "risk_amount": plan.risk_amount,
                        "notional": plan.notional,
                        "leverage_equivalent": plan.leverage_equivalent,
                    },
                },
            ),
            on_conflict=("client_order_id",),
        )

    def save_fill(self, fill: UserTradeFill, *, client_order_id: str) -> None:
        self._rest.upsert(
            "fills",
            (
                {
                    "symbol": fill.symbol,
                    "venue_trade_id": fill.trade_id,
                    "venue_order_id": fill.order_id,
                    "client_order_id": client_order_id,
                    "side": fill.side.upper(),
                    "position_side": fill.position_side,
                    "qty": fill.qty,
                    "price": fill.price,
                    "quote_qty": fill.quote_qty,
                    "realized_pnl": fill.realized_pnl,
                    "commission": fill.commission,
                    "commission_asset": fill.commission_asset,
                    "maker": fill.maker,
                    "time_ms": fill.time_ms,
                    "raw": {"buyer": fill.buyer},
                },
            ),
            on_conflict=("symbol", "venue_trade_id"),
        )

    def save_open_position(
        self,
        *,
        plan: EntryOrderPlan,
        position: PositionSnapshot,
        entry_time_ms: int,
        filled_qty: Decimal,
        average_entry_price: Decimal,
    ) -> str:
        direction = TradeDirection.LONG if plan.side == "Buy" else TradeDirection.SHORT
        if position.symbol != plan.symbol or not position.is_open:
            raise PersistenceError("authoritative open position does not match entry plan")
        position_id = stable_position_id_from_episode(
            plan.symbol,
            direction,
            entry_time_ms,
        )
        mark = position.mark_price or average_entry_price
        self._rest.upsert(
            "positions",
            (
                {
                    "position_id": position_id,
                    "signal_id": plan.signal_id,
                    "venue": "BINANCE",
                    "environment": "DEMO",
                    "symbol": plan.symbol,
                    "direction": direction,
                    "state": "OPEN",
                    "entry_qty": filled_qty,
                    "remaining_qty": position.size,
                    "average_entry_price": average_entry_price,
                    "initial_stop_loss": plan.stop_loss,
                    "tp1": plan.take_profit_1,
                    "tp2": plan.take_profit_2,
                    "leverage": position.leverage,
                    "opened_at_ms": entry_time_ms,
                    "latest_mark_price": mark,
                    "unrealized_pnl": position.unrealised_pnl,
                    "updated_at_ms": max(entry_time_ms, position.updated_time_ms or entry_time_ms),
                    "source": {
                        "identity_chain": "DISCOVERY_SIGNAL_GEOMETRY_ORDER_FILL_POSITION",
                        "client_order_id": plan.order_link_id,
                    },
                },
            ),
            on_conflict=("position_id",),
        )
        return position_id

    def resolve_context(
        self,
        *,
        symbol: str,
        direction: TradeDirection,
        entry_time_ms: int,
    ) -> DurableTradeContext:
        position_id = stable_position_id_from_episode(symbol, direction, entry_time_ms)
        rows = self._rest.select(
            "positions",
            params={
                "select": "position_id,signal_id,initial_stop_loss",
                "position_id": f"eq.{position_id}",
                "limit": "1",
            },
        )
        if not rows:
            return DurableTradeContext(position_id, None, None, None, None, False)
        position = rows[0]
        stop_raw = position.get("initial_stop_loss")
        initial_stop = Decimal(str(stop_raw)) if stop_raw is not None else None
        signal_raw = position.get("signal_id")
        signal_id = str(signal_raw) if signal_raw else None
        if not signal_id:
            return DurableTradeContext(
                position_id,
                None,
                initial_stop,
                None,
                None,
                False,
            )

        signals = self._rest.select(
            "signals",
            params={
                "select": "signal_id,setup,regime,status",
                "signal_id": f"eq.{signal_id}",
                "limit": "1",
            },
        )
        geometry = self._rest.select(
            "signal_geometry",
            params={
                "select": "signal_id,stop_loss",
                "signal_id": f"eq.{signal_id}",
                "limit": "1",
            },
        )
        setup = str(signals[0].get("setup")) if signals and signals[0].get("setup") else None
        regime = str(signals[0].get("regime")) if signals and signals[0].get("regime") else None
        status = str(signals[0].get("status")) if signals else None
        geometry_stop = (
            Decimal(str(geometry[0].get("stop_loss")))
            if geometry and geometry[0].get("stop_loss") is not None
            else None
        )
        if initial_stop is None:
            initial_stop = geometry_stop
        stop_matches = (
            initial_stop is not None
            and geometry_stop is not None
            and initial_stop == geometry_stop
        )
        eligible = bool(setup and regime and status == "EXECUTION_READY" and stop_matches)
        return DurableTradeContext(
            position_id=position_id,
            signal_id=signal_id,
            initial_stop_loss=initial_stop,
            setup=setup,
            regime=regime,
            calibration_eligible=eligible,
        )
