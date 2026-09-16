from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from crypto_scanner.binance.public_rest import BinanceDemoPublicRestClient
from crypto_scanner.config import load_runtime_config
from crypto_scanner.funding_oi_demo import FUNDING_OI_STRATEGY_ID
from crypto_scanner.persistence import (
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseRestClient,
    read_with_retry,
)
from crypto_scanner.regime_specialist_demo import REGIME_SPECIALIST_STRATEGY_ID
from crypto_scanner.transaction_costs import BASELINE_ROUND_TRIP_COST_BPS
from crypto_scanner.volatility_breakout_demo import VOLATILITY_BREAKOUT_STRATEGY_ID

FORWARD_STATE_KEY = "strategy_forward_evidence_v1"
FORWARD_SCHEMA_VERSION = "strategy-forward-evidence-v1"
WORKER_COMPONENT = "STRATEGY_FORWARD_EVIDENCE"
EXECUTION_INTERVAL = "5"
EXECUTION_INTERVAL_MS = 5 * 60_000
SIGNAL_LOOKBACK_MS = 30 * 24 * 60 * 60_000
MAX_SIGNAL_PAGES = 20
PAGE_SIZE = 500

_STRATEGY_TIMEFRAMES = {
    REGIME_SPECIALIST_STRATEGY_ID: "D1",
    VOLATILITY_BREAKOUT_STRATEGY_ID: "D1",
    FUNDING_OI_STRATEGY_ID: "D1",
}


@dataclass(frozen=True, slots=True)
class ForwardMetrics:
    sample_size: int
    wins: int
    losses: int
    win_rate: Decimal
    expectancy_r: Decimal
    profit_factor: Decimal | None
    max_drawdown_r: Decimal
    average_mfe_r: Decimal
    average_mae_r: Decimal
    tp1_touch_rate: Decimal
    average_holding_ms: int

    def to_dict(self) -> dict[str, object]:
        return {
            "sample_size": self.sample_size,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": str(self.win_rate),
            "expectancy_r": str(self.expectancy_r),
            "profit_factor": None if self.profit_factor is None else str(self.profit_factor),
            "max_drawdown_r": str(self.max_drawdown_r),
            "average_mfe_r": str(self.average_mfe_r),
            "average_mae_r": str(self.average_mae_r),
            "tp1_touch_rate": str(self.tp1_touch_rate),
            "average_holding_ms": self.average_holding_ms,
        }


class _ForwardRest(SupabaseRestClient):
    _TABLES = frozenset(
        {
            "signals",
            "signal_geometry",
            "strategy_forward_evaluations",
            "strategy_paper_trades",
        }
    )

    def select_rows(
        self,
        table: str,
        params: dict[str, str],
        *,
        operation: str,
    ) -> tuple[dict[str, object], ...]:
        if table not in self._TABLES:
            raise PersistenceError("forward evidence table is not allow-listed")
        response = read_with_retry(
            self._client,
            f"{self.base_url}/rest/v1/{table}",
            params=params,
            operation=operation,
            headers=self._headers(),
        )
        if response.is_error:
            raise PersistenceError(
                f"forward evidence read failed table={table} status={response.status_code}"
            )
        payload = response.json()
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise PersistenceError(f"forward evidence payload is invalid table={table}")
        return tuple(payload)

    def patch_paper(self, paper_trade_id: str, fields: dict[str, object]) -> None:
        if not paper_trade_id.startswith("paper-"):
            raise PersistenceError("paper trade identity is invalid")
        response = self._client.patch(
            f"{self.base_url}/rest/v1/strategy_paper_trades",
            params={"paper_trade_id": f"eq.{paper_trade_id}"},
            headers=self._headers(prefer="return=minimal"),
            json=fields,
        )
        if response.is_error:
            raise PersistenceError(
                "paper lifecycle update failed "
                f"status={response.status_code}: {response.text[:300]}"
            )


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(part) for part in parts).encode()
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:32]}"


def _strategy_timeframe(strategy_id: str, evidence: dict[str, object]) -> str:
    explicit = evidence.get("strategy_timeframe") or evidence.get("timeframe")
    if explicit not in (None, ""):
        return str(explicit).upper()
    return _STRATEGY_TIMEFRAMES.get(strategy_id, "M3_M5")


def _decimal(value: object | None) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _fetch_recent_strategy_signals(
    rest: _ForwardRest,
    *,
    now_ms: int,
) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    cutoff = max(0, now_ms - SIGNAL_LOOKBACK_MS)
    for page in range(MAX_SIGNAL_PAGES):
        batch = rest.select_rows(
            "signals",
            {
                "select": (
                    "signal_id,symbol,direction,setup,regime,status,score,created_at_ms,evidence"
                ),
                "created_at_ms": f"gte.{cutoff}",
                "order": "created_at_ms.asc",
                "limit": str(PAGE_SIZE),
                "offset": str(page * PAGE_SIZE),
            },
            operation="FORWARD_SIGNALS",
        )
        for row in batch:
            evidence = row.get("evidence")
            if isinstance(evidence, dict) and evidence.get("strategy_id"):
                rows.append(row)
        if len(batch) < PAGE_SIZE:
            break
    return tuple(rows)


def _geometry_by_signal(
    rest: _ForwardRest,
    signal_ids: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    output: dict[str, dict[str, object]] = {}
    for signal_id in signal_ids:
        rows = rest.select_rows(
            "signal_geometry",
            {
                "select": (
                    "signal_id,entry_mode,entry_price,stop_loss,tp1,tp2,risk_per_unit,"
                    "rr_tp1,rr_tp2,geometry_created_at_ms,raw"
                ),
                "signal_id": f"eq.{signal_id}",
                "limit": "1",
            },
            operation="FORWARD_SIGNAL_GEOMETRY",
        )
        if rows:
            output[signal_id] = rows[0]
    return output


def _evaluation_rows(
    signals: tuple[dict[str, object], ...],
    geometry: dict[str, dict[str, object]],
) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for signal in signals:
        signal_id = str(signal.get("signal_id") or "")
        geo = geometry.get(signal_id)
        evidence = signal.get("evidence")
        if not signal_id or geo is None or not isinstance(evidence, dict):
            continue
        strategy_id = str(evidence.get("strategy_id") or "").strip()
        if not strategy_id:
            continue
        entry = _decimal(geo.get("entry_price"))
        stop = _decimal(geo.get("stop_loss"))
        tp2 = _decimal(geo.get("tp2"))
        rr2 = _decimal(geo.get("rr_tp2"))
        if entry is None or stop is None or tp2 is None or rr2 is None:
            continue
        promotion_stage = str(evidence.get("promotion_stage") or "UNSPECIFIED")
        evaluation_id = _stable_id("eval", signal_id, strategy_id)
        rows.append(
            {
                "evaluation_id": evaluation_id,
                "source_signal_id": signal_id,
                "strategy_id": strategy_id,
                "promotion_stage": promotion_stage,
                "symbol": str(signal["symbol"]).upper(),
                "direction": str(signal["direction"]).upper(),
                "strategy_timeframe": _strategy_timeframe(strategy_id, evidence),
                "execution_timeframe": EXECUTION_INTERVAL,
                "setup": str(signal.get("setup") or "UNKNOWN"),
                "regime": str(signal.get("regime") or "UNKNOWN"),
                "signal_created_at_ms": int(signal["created_at_ms"]),
                "score": signal.get("score"),
                "planned_entry_price": entry,
                "planned_stop_loss": stop,
                "planned_tp1": _decimal(geo.get("tp1")),
                "planned_tp2": tp2,
                "planned_rr_tp2": rr2,
                "metadata": {
                    "schema_version": FORWARD_SCHEMA_VERSION,
                    "source_signal_status": signal.get("status"),
                    "entry_mode": geo.get("entry_mode"),
                    "geometry_created_at_ms": geo.get("geometry_created_at_ms"),
                    "execution_influence": False,
                    "promotion_authority": False,
                    "paper_entry_policy": "NEXT_5M_OPEN",
                    "intrabar_conflict_policy": "SL_FIRST",
                    "round_trip_cost_bps": str(BASELINE_ROUND_TRIP_COST_BPS),
                },
            }
        )
    return tuple(rows)


def _paper_seed_rows(evaluations: tuple[dict[str, object], ...]) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "paper_trade_id": _stable_id("paper", row["evaluation_id"]),
            "evaluation_id": row["evaluation_id"],
            "strategy_id": row["strategy_id"],
            "promotion_stage": row["promotion_stage"],
            "symbol": row["symbol"],
            "direction": row["direction"],
            "strategy_timeframe": row["strategy_timeframe"],
            "execution_timeframe": EXECUTION_INTERVAL,
            "regime": row["regime"],
            "decision_time_ms": row["signal_created_at_ms"],
            "status": "PENDING",
            "stop_loss": row["planned_stop_loss"],
            "tp1": row.get("planned_tp1"),
            "tp2": row["planned_tp2"],
            "round_trip_cost_bps": BASELINE_ROUND_TRIP_COST_BPS,
            "metadata": {
                "schema_version": FORWARD_SCHEMA_VERSION,
                "source_signal_id": row["source_signal_id"],
                "planned_entry_price": str(row["planned_entry_price"]),
                "planned_rr_tp2": str(row["planned_rr_tp2"]),
                "execution_influence": False,
                "promotion_authority": False,
            },
        }
        for row in evaluations
    )


def sync_signal_evaluations(
    rest: _ForwardRest,
    *,
    now_ms: int,
) -> tuple[int, int]:
    signals = _fetch_recent_strategy_signals(rest, now_ms=now_ms)
    geometry = _geometry_by_signal(
        rest,
        tuple(str(row["signal_id"]) for row in signals if row.get("signal_id")),
    )
    evaluations = _evaluation_rows(signals, geometry)
    if evaluations:
        rest.upsert(
            "strategy_forward_evaluations",
            evaluations,
            on_conflict=("evaluation_id",),
        )
        rest.upsert(
            "strategy_paper_trades",
            _paper_seed_rows(evaluations),
            on_conflict=("paper_trade_id",),
        )
    return len(evaluations), len(signals)


def _open_papers(rest: _ForwardRest) -> tuple[dict[str, object], ...]:
    return rest.select_rows(
        "strategy_paper_trades",
        {
            "select": (
                "paper_trade_id,evaluation_id,strategy_id,promotion_stage,symbol,direction,"
                "strategy_timeframe,execution_timeframe,regime,decision_time_ms,status,"
                "entry_time_ms,entry_price,stop_loss,tp1,tp2,initial_risk,exit_time_ms,"
                "exit_price,exit_reason,gross_result_r,net_result_r,round_trip_cost_bps,"
                "mfe_r,mae_r,tp1_touched,last_observed_ms,metadata"
            ),
            "status": "in.(PENDING,OPEN)",
            "order": "decision_time_ms.asc",
            "limit": "1000",
        },
        operation="FORWARD_OPEN_PAPERS",
    )


def _valid_geometry(direction: str, entry: Decimal, stop: Decimal, tp2: Decimal) -> bool:
    if direction == "LONG":
        return stop < entry < tp2
    if direction == "SHORT":
        return tp2 < entry < stop
    return False


def _activate_pending(
    rest: _ForwardRest,
    market: BinanceDemoPublicRestClient,
    row: dict[str, object],
    *,
    now_ms: int,
) -> dict[str, object] | None:
    decision_time = int(row["decision_time_ms"])
    if now_ms <= decision_time:
        return None
    candles = market.get_klines_window(
        str(row["symbol"]),
        EXECUTION_INTERVAL,
        start_time_ms=decision_time,
        end_time_ms=now_ms + 1,
        max_candles=500,
    )
    entry_bar = next((item for item in candles if item.start_time_ms > decision_time), None)
    if entry_bar is None:
        return None
    entry = entry_bar.open
    stop = Decimal(str(row["stop_loss"]))
    tp2 = Decimal(str(row["tp2"]))
    direction = str(row["direction"])
    if not _valid_geometry(direction, entry, stop, tp2):
        rest.patch_paper(
            str(row["paper_trade_id"]),
            {
                "status": "INVALID",
                "entry_time_ms": entry_bar.start_time_ms,
                "entry_price": str(entry),
                "exit_reason": "NEXT_BAR_OPEN_INVALIDATED_FROZEN_GEOMETRY",
                "last_observed_ms": entry_bar.start_time_ms,
            },
        )
        return None
    risk = abs(entry - stop)
    rest.patch_paper(
        str(row["paper_trade_id"]),
        {
            "status": "OPEN",
            "entry_time_ms": entry_bar.start_time_ms,
            "entry_price": str(entry),
            "initial_risk": str(risk),
            "mfe_r": "0",
            "mae_r": "0",
            "last_observed_ms": entry_bar.start_time_ms - EXECUTION_INTERVAL_MS,
        },
    )
    return {
        **row,
        "status": "OPEN",
        "entry_time_ms": entry_bar.start_time_ms,
        "entry_price": str(entry),
        "initial_risk": str(risk),
        "mfe_r": "0",
        "mae_r": "0",
        "last_observed_ms": entry_bar.start_time_ms - EXECUTION_INTERVAL_MS,
    }


def _advance_open(
    rest: _ForwardRest,
    market: BinanceDemoPublicRestClient,
    row: dict[str, object],
    *,
    now_ms: int,
) -> bool:
    entry_time = int(row["entry_time_ms"])
    entry = Decimal(str(row["entry_price"]))
    stop = Decimal(str(row["stop_loss"]))
    tp1 = _decimal(row.get("tp1"))
    tp2 = Decimal(str(row["tp2"]))
    risk = Decimal(str(row["initial_risk"]))
    direction = str(row["direction"])
    last_observed = int(row.get("last_observed_ms") or (entry_time - EXECUTION_INTERVAL_MS))
    start = max(entry_time, last_observed + EXECUTION_INTERVAL_MS)
    if start >= now_ms:
        return False
    candles = market.get_klines_window(
        str(row["symbol"]),
        EXECUTION_INTERVAL,
        start_time_ms=start,
        end_time_ms=now_ms + 1,
        max_candles=50_000,
    )
    closed = tuple(
        candle
        for candle in candles
        if candle.start_time_ms + EXECUTION_INTERVAL_MS <= now_ms
    )
    if not closed:
        return False

    mfe = Decimal(str(row.get("mfe_r") or 0))
    mae = Decimal(str(row.get("mae_r") or 0))
    tp1_touched = bool(row.get("tp1_touched", False))
    exit_reason: str | None = None
    exit_price: Decimal | None = None
    exit_time: int | None = None
    gross_result: Decimal | None = None

    for candle in closed:
        if direction == "LONG":
            mfe = max(mfe, max(Decimal(0), candle.high - entry) / risk)
            mae = max(mae, max(Decimal(0), entry - candle.low) / risk)
            if tp1 is not None and candle.high >= tp1:
                tp1_touched = True
            stop_hit = candle.low <= stop
            target_hit = candle.high >= tp2
            if stop_hit:
                exit_reason, exit_price, gross_result = "STOP_LOSS", stop, Decimal("-1")
            elif target_hit:
                exit_reason = "TAKE_PROFIT_2"
                exit_price = tp2
                gross_result = (tp2 - entry) / risk
        else:
            mfe = max(mfe, max(Decimal(0), entry - candle.low) / risk)
            mae = max(mae, max(Decimal(0), candle.high - entry) / risk)
            if tp1 is not None and candle.low <= tp1:
                tp1_touched = True
            stop_hit = candle.high >= stop
            target_hit = candle.low <= tp2
            if stop_hit:
                exit_reason, exit_price, gross_result = "STOP_LOSS", stop, Decimal("-1")
            elif target_hit:
                exit_reason = "TAKE_PROFIT_2"
                exit_price = tp2
                gross_result = (entry - tp2) / risk
        if exit_reason is not None:
            exit_time = candle.start_time_ms
            break

    patch: dict[str, object] = {
        "mfe_r": str(mfe),
        "mae_r": str(mae),
        "tp1_touched": tp1_touched,
        "last_observed_ms": closed[-1].start_time_ms,
    }
    if exit_reason is not None:
        assert exit_price is not None and exit_time is not None and gross_result is not None
        cost_bps = Decimal(str(row.get("round_trip_cost_bps") or BASELINE_ROUND_TRIP_COST_BPS))
        cost_r = entry * cost_bps / Decimal("10000") / risk
        patch.update(
            {
                "status": "CLOSED",
                "exit_time_ms": exit_time,
                "exit_price": str(exit_price),
                "exit_reason": exit_reason,
                "gross_result_r": str(gross_result),
                "net_result_r": str(gross_result - cost_r),
            }
        )
    rest.patch_paper(str(row["paper_trade_id"]), patch)
    return exit_reason is not None


def advance_paper_lifecycle(
    rest: _ForwardRest,
    market: BinanceDemoPublicRestClient,
    *,
    now_ms: int,
) -> tuple[int, int, int]:
    opened = 0
    closed = 0
    failures = 0
    for original in _open_papers(rest):
        try:
            row = original
            if str(row["status"]) == "PENDING":
                activated = _activate_pending(rest, market, row, now_ms=now_ms)
                if activated is None:
                    continue
                row = activated
                opened += 1
            if str(row["status"]) == "OPEN" and _advance_open(rest, market, row, now_ms=now_ms):
                closed += 1
        except Exception as exc:  # isolate one symbol/paper from the 24/7 observer
            failures += 1
            print(
                json.dumps(
                    {
                        "component": WORKER_COMPONENT,
                        "paper_trade_id": original.get("paper_trade_id"),
                        "symbol": original.get("symbol"),
                        "error_class": type(exc).__name__,
                    },
                    sort_keys=True,
                )
            )
    return opened, closed, failures


def _all_closed_papers(rest: _ForwardRest) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for page in range(20):
        batch = rest.select_rows(
            "strategy_paper_trades",
            {
                "select": (
                    "paper_trade_id,strategy_id,promotion_stage,symbol,direction,"
                    "strategy_timeframe,regime,entry_time_ms,exit_time_ms,net_result_r,"
                    "mfe_r,mae_r,tp1_touched"
                ),
                "status": "eq.CLOSED",
                "order": "exit_time_ms.asc",
                "limit": str(PAGE_SIZE),
                "offset": str(page * PAGE_SIZE),
            },
            operation="FORWARD_CLOSED_PAPERS",
        )
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return tuple(rows)


def calculate_forward_metrics(rows: tuple[dict[str, object], ...]) -> ForwardMetrics:
    if not rows:
        return ForwardMetrics(0, 0, 0, Decimal(0), Decimal(0), None, Decimal(0), Decimal(0), Decimal(0), Decimal(0), 0)
    ordered = sorted(rows, key=lambda row: int(row["exit_time_ms"]))
    results = tuple(Decimal(str(row["net_result_r"])) for row in ordered)
    wins = sum(result > 0 for result in results)
    losses = sum(result < 0 for result in results)
    gross_profit = sum((value for value in results if value > 0), Decimal(0))
    gross_loss = abs(sum((value for value in results if value < 0), Decimal(0)))
    equity = Decimal(0)
    peak = Decimal(0)
    max_drawdown = Decimal(0)
    for value in results:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    count = Decimal(len(rows))
    holding_values = tuple(
        int(row["exit_time_ms"]) - int(row["entry_time_ms"])
        for row in ordered
        if row.get("entry_time_ms") is not None
    )
    return ForwardMetrics(
        sample_size=len(rows),
        wins=wins,
        losses=losses,
        win_rate=Decimal(wins) / count,
        expectancy_r=sum(results, Decimal(0)) / count,
        profit_factor=gross_profit / gross_loss if gross_loss > 0 else None,
        max_drawdown_r=max_drawdown,
        average_mfe_r=sum((Decimal(str(row.get("mfe_r") or 0)) for row in rows), Decimal(0)) / count,
        average_mae_r=sum((Decimal(str(row.get("mae_r") or 0)) for row in rows), Decimal(0)) / count,
        tp1_touch_rate=Decimal(sum(bool(row.get("tp1_touched")) for row in rows)) / count,
        average_holding_ms=(sum(holding_values) // len(holding_values)) if holding_values else 0,
    )


def _slice_summary(rows: tuple[dict[str, object], ...], keys: tuple[str, ...]) -> dict[str, object]:
    grouped: dict[tuple[str, ...], list[dict[str, object]]] = {}
    for row in rows:
        key = tuple(str(row.get(name) or "UNKNOWN") for name in keys)
        grouped.setdefault(key, []).append(row)
    return {
        "|".join(key): calculate_forward_metrics(tuple(group)).to_dict()
        for key, group in sorted(grouped.items())
    }


def build_forward_summary(rows: tuple[dict[str, object], ...], *, generated_at_ms: int) -> dict[str, object]:
    return {
        "schema_version": FORWARD_SCHEMA_VERSION,
        "generated_at_ms": generated_at_ms,
        "closed_paper_trades": len(rows),
        "execution_influence": False,
        "promotion_authority": False,
        "entry_policy": "NEXT_5M_OPEN",
        "intrabar_conflict_policy": "SL_FIRST",
        "round_trip_cost_bps": str(BASELINE_ROUND_TRIP_COST_BPS),
        "by_strategy": _slice_summary(rows, ("strategy_id",)),
        "by_strategy_symbol": _slice_summary(rows, ("strategy_id", "symbol")),
        "by_strategy_timeframe": _slice_summary(rows, ("strategy_id", "strategy_timeframe")),
        "by_strategy_regime": _slice_summary(rows, ("strategy_id", "regime")),
        "by_strategy_direction": _slice_summary(rows, ("strategy_id", "direction")),
        "by_full_slice": _slice_summary(
            rows,
            ("strategy_id", "symbol", "strategy_timeframe", "regime", "direction"),
        ),
    }


def paper_forward_results(
    config: SupabasePersistenceConfig,
    strategy_id: str,
    *,
    promotion_stage: str,
) -> tuple[tuple[Decimal, ...], dict[str, object]]:
    if not config.enabled:
        return (), {"paper_forward_available": False}
    with _ForwardRest(config) as rest:
        rows = rest.select_rows(
            "strategy_paper_trades",
            {
                "select": "exit_time_ms,net_result_r",
                "strategy_id": f"eq.{strategy_id}",
                "promotion_stage": f"eq.{promotion_stage}",
                "status": "eq.CLOSED",
                "order": "exit_time_ms.asc",
                "limit": "1000",
            },
            operation="PAPER_FORWARD_PROMOTION_EVIDENCE",
        )
    results = tuple(Decimal(str(row["net_result_r"])) for row in rows)
    duration = (
        int(rows[-1]["exit_time_ms"]) - int(rows[0]["exit_time_ms"])
        if len(rows) >= 2
        else None
    )
    return results, {
        "paper_forward_available": True,
        "paper_closed_count": len(results),
        "paper_evidence_duration_ms": duration,
        "execution_influence": False,
        "promotion_authority": False,
    }


def run_forward_evidence_cycle(*, now_ms: int | None = None) -> dict[str, object]:
    timestamp = _now_ms() if now_ms is None else now_ms
    config = SupabasePersistenceConfig.from_environment()
    if not config.enabled:
        raise PersistenceError("strategy forward evidence requires dedicated Crypto Scanner Supabase")
    runtime = load_runtime_config()

    with (
        _ForwardRest(config) as rest,
        BinanceDemoPublicRestClient(base_url=runtime.binance_rest_url) as market,
    ):
        evaluations, source_signals = sync_signal_evaluations(rest, now_ms=timestamp)
        opened, closed_now, paper_failures = advance_paper_lifecycle(
            rest,
            market,
            now_ms=timestamp,
        )
        closed_rows = _all_closed_papers(rest)
        summary = build_forward_summary(closed_rows, generated_at_ms=timestamp)
        rest.upsert(
            "runtime_state",
            (
                {
                    "state_key": FORWARD_STATE_KEY,
                    "version": 1,
                    "state": summary,
                    "updated_at_ms": timestamp,
                },
            ),
            on_conflict=("state_key",),
        )
        rest.upsert(
            "heartbeats",
            (
                {
                    "component": WORKER_COMPONENT,
                    "observed_at_ms": timestamp,
                    "status": "SUCCESS" if paper_failures == 0 else "DEGRADED",
                    "git_sha": os.getenv("GITHUB_SHA", "LOCAL"),
                    "details": {
                        "schema_version": FORWARD_SCHEMA_VERSION,
                        "source_strategy_signals": source_signals,
                        "evaluations_synced": evaluations,
                        "paper_opened": opened,
                        "paper_closed_now": closed_now,
                        "paper_failures": paper_failures,
                        "closed_sample_total": len(closed_rows),
                        "execution_influence": False,
                        "promotion_authority": False,
                        "live_trading_locked": True,
                    },
                },
            ),
            on_conflict=("component",),
        )

    return {
        "status": "PASS_STRATEGY_FORWARD_EVIDENCE" if paper_failures == 0 else "DEGRADED_STRATEGY_FORWARD_EVIDENCE",
        "source_strategy_signals": source_signals,
        "evaluations_synced": evaluations,
        "paper_opened": opened,
        "paper_closed_now": closed_now,
        "paper_failures": paper_failures,
        "closed_sample_total": len(closed_rows),
        "execution_influence": False,
        "promotion_authority": False,
        "live_trading_locked": True,
    }


def main() -> None:
    print(json.dumps(run_forward_evidence_cycle(), indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
