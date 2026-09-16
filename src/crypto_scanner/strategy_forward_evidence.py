from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal

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
BAR_MS = 300_000
LOOKBACK_MS = 30 * 86_400_000
PAGE_SIZE = 500

_STRATEGY_TIMEFRAME = {
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


class ForwardRest(SupabaseRestClient):
    _READ_TABLES = frozenset(
        {
            "signals",
            "signal_geometry",
            "strategy_forward_evaluations",
            "strategy_paper_trades",
            "runtime_state",
        }
    )

    def select(
        self,
        table: str,
        params: dict[str, str],
        *,
        operation: str,
    ) -> tuple[dict[str, object], ...]:
        if table not in self._READ_TABLES:
            raise PersistenceError("forward-evidence table is not allow-listed")
        response = read_with_retry(
            self._client,
            f"{self.base_url}/rest/v1/{table}",
            params=params,
            operation=operation,
            headers=self._headers(),
        )
        if response.is_error:
            raise PersistenceError(
                f"forward-evidence read failed table={table} status={response.status_code}"
            )
        payload = response.json()
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise PersistenceError(f"invalid forward-evidence payload table={table}")
        return tuple(payload)

    def insert_ignore(
        self,
        table: str,
        rows: tuple[dict[str, object], ...],
        *,
        on_conflict: str,
    ) -> None:
        if not rows:
            return
        # REST JSON must not receive Decimal objects directly. String encoding also
        # preserves exact financial values rather than converting through float.
        payload = json.loads(json.dumps(rows, default=str))
        response = self._client.post(
            f"{self.base_url}/rest/v1/{table}",
            params={"on_conflict": on_conflict},
            headers=self._headers(prefer="resolution=ignore-duplicates,return=minimal"),
            json=payload,
        )
        if response.is_error:
            raise PersistenceError(
                f"forward-evidence insert failed table={table} status={response.status_code}: "
                f"{response.text[:300]}"
            )

    def patch_paper(self, paper_id: str, fields: dict[str, object]) -> None:
        response = self._client.patch(
            f"{self.base_url}/rest/v1/strategy_paper_trades",
            params={"paper_trade_id": f"eq.{paper_id}"},
            headers=self._headers(prefer="return=minimal"),
            json=fields,
        )
        if response.is_error:
            raise PersistenceError(
                f"paper update failed status={response.status_code}: {response.text[:300]}"
            )


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(part) for part in parts).encode()
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:32]}"


def _dec(value: object | None) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def _strategy_tf(strategy_id: str, evidence: dict[str, object]) -> str:
    explicit = evidence.get("strategy_timeframe") or evidence.get("timeframe")
    if explicit not in (None, ""):
        return str(explicit).upper()
    return _STRATEGY_TIMEFRAME.get(strategy_id, "M3_M5")


def ensure_observer_start(rest: ForwardRest, *, now_ms: int) -> int:
    rows = rest.select(
        "runtime_state",
        {
            "select": "version,state",
            "state_key": f"eq.{FORWARD_STATE_KEY}",
            "limit": "1",
        },
        operation="STRATEGY_FORWARD_STATE",
    )
    if rows:
        state = rows[0].get("state")
        if not isinstance(state, dict):
            raise PersistenceError("strategy forward runtime state is malformed")
        started = int(state.get("observer_started_at_ms") or 0)
        if started <= 0:
            raise PersistenceError("strategy forward observer start is missing")
        return started

    initial = {
        "schema_version": FORWARD_SCHEMA_VERSION,
        "observer_started_at_ms": now_ms,
        "generated_at_ms": now_ms,
        "closed_paper_trades": 0,
        "entry_policy": "NEXT_5M_OPEN",
        "intrabar_conflict_policy": "SL_FIRST",
        "round_trip_cost_bps": str(BASELINE_ROUND_TRIP_COST_BPS),
        "execution_influence": False,
        "promotion_authority": False,
        "by_strategy": {},
        "by_strategy_symbol": {},
        "by_strategy_timeframe": {},
        "by_strategy_regime": {},
        "by_strategy_direction": {},
        "by_full_slice": {},
    }
    rest.upsert(
        "runtime_state",
        (
            {
                "state_key": FORWARD_STATE_KEY,
                "version": 1,
                "state": initial,
                "updated_at_ms": now_ms,
            },
        ),
        on_conflict=("state_key",),
    )
    return now_ms


def _recent_signals(
    rest: ForwardRest,
    *,
    now_ms: int,
    floor_ms: int,
) -> tuple[dict[str, object], ...]:
    output: list[dict[str, object]] = []
    cutoff = max(floor_ms, now_ms - LOOKBACK_MS)
    for offset in range(0, 10_000, PAGE_SIZE):
        page = rest.select(
            "signals",
            {
                "select": (
                    "signal_id,symbol,direction,setup,regime,status,score,created_at_ms,evidence"
                ),
                "created_at_ms": f"gte.{cutoff}",
                "order": "created_at_ms.asc",
                "limit": str(PAGE_SIZE),
                "offset": str(offset),
            },
            operation="STRATEGY_FORWARD_SIGNALS",
        )
        output.extend(
            row
            for row in page
            if isinstance(row.get("evidence"), dict)
            and dict(row["evidence"]).get("strategy_id")
        )
        if len(page) < PAGE_SIZE:
            break
    return tuple(output)


def _existing_source_signal_ids(
    rest: ForwardRest,
    *,
    floor_ms: int,
) -> frozenset[str]:
    output: list[str] = []
    for offset in range(0, 10_000, PAGE_SIZE):
        page = rest.select(
            "strategy_forward_evaluations",
            {
                "select": "source_signal_id",
                "signal_created_at_ms": f"gte.{floor_ms}",
                "order": "signal_created_at_ms.asc",
                "limit": str(PAGE_SIZE),
                "offset": str(offset),
            },
            operation="STRATEGY_FORWARD_EXISTING_EVALUATIONS",
        )
        output.extend(str(row["source_signal_id"]) for row in page if row.get("source_signal_id"))
        if len(page) < PAGE_SIZE:
            break
    return frozenset(output)


def _geometry(rest: ForwardRest, signal_id: str) -> dict[str, object] | None:
    rows = rest.select(
        "signal_geometry",
        {
            "select": (
                "signal_id,entry_mode,entry_price,stop_loss,tp1,tp2,rr_tp2,geometry_created_at_ms"
            ),
            "signal_id": f"eq.{signal_id}",
            "limit": "1",
        },
        operation="STRATEGY_FORWARD_GEOMETRY",
    )
    return rows[0] if rows else None


def sync_signal_evaluations(
    rest: ForwardRest,
    *,
    now_ms: int,
    observer_started_at_ms: int,
) -> tuple[int, int]:
    source = _recent_signals(
        rest,
        now_ms=now_ms,
        floor_ms=observer_started_at_ms,
    )
    existing = _existing_source_signal_ids(
        rest,
        floor_ms=observer_started_at_ms,
    )
    new_source = tuple(row for row in source if str(row.get("signal_id") or "") not in existing)
    evaluations: list[dict[str, object]] = []
    papers: list[dict[str, object]] = []
    for signal in new_source:
        signal_id = str(signal.get("signal_id") or "")
        evidence_raw = signal.get("evidence")
        if not signal_id or not isinstance(evidence_raw, dict):
            continue
        evidence = dict(evidence_raw)
        strategy_id = str(evidence.get("strategy_id") or "").strip()
        geo = _geometry(rest, signal_id)
        if not strategy_id or geo is None:
            continue
        entry = _dec(geo.get("entry_price"))
        stop = _dec(geo.get("stop_loss"))
        tp2 = _dec(geo.get("tp2"))
        rr2 = _dec(geo.get("rr_tp2"))
        if None in (entry, stop, tp2, rr2):
            continue
        assert entry is not None and stop is not None and tp2 is not None and rr2 is not None
        stage = str(evidence.get("promotion_stage") or "UNSPECIFIED")
        evaluation_id = _id("eval", signal_id, strategy_id)
        common = {
            "strategy_id": strategy_id,
            "promotion_stage": stage,
            "symbol": str(signal["symbol"]).upper(),
            "direction": str(signal["direction"]).upper(),
            "strategy_timeframe": _strategy_tf(strategy_id, evidence),
            "execution_timeframe": EXECUTION_INTERVAL,
            "regime": str(signal.get("regime") or "UNKNOWN"),
        }
        evaluations.append(
            {
                "evaluation_id": evaluation_id,
                "source_signal_id": signal_id,
                **common,
                "setup": str(signal.get("setup") or "UNKNOWN"),
                "signal_created_at_ms": int(signal["created_at_ms"]),
                "score": signal.get("score"),
                "planned_entry_price": entry,
                "planned_stop_loss": stop,
                "planned_tp1": _dec(geo.get("tp1")),
                "planned_tp2": tp2,
                "planned_rr_tp2": rr2,
                "metadata": {
                    "schema_version": FORWARD_SCHEMA_VERSION,
                    "source_signal_status": signal.get("status"),
                    "entry_mode": geo.get("entry_mode"),
                    "geometry_created_at_ms": geo.get("geometry_created_at_ms"),
                    "entry_policy": "NEXT_5M_OPEN",
                    "intrabar_conflict_policy": "SL_FIRST",
                    "execution_influence": False,
                    "promotion_authority": False,
                },
            }
        )
        papers.append(
            {
                "paper_trade_id": _id("paper", evaluation_id),
                "evaluation_id": evaluation_id,
                **common,
                "decision_time_ms": int(signal["created_at_ms"]),
                "status": "PENDING",
                "stop_loss": stop,
                "tp1": _dec(geo.get("tp1")),
                "tp2": tp2,
                "round_trip_cost_bps": BASELINE_ROUND_TRIP_COST_BPS,
                "metadata": {
                    "schema_version": FORWARD_SCHEMA_VERSION,
                    "source_signal_id": signal_id,
                    "planned_entry_price": str(entry),
                    "planned_rr_tp2": str(rr2),
                    "execution_influence": False,
                    "promotion_authority": False,
                },
            }
        )
    # Seeds are immutable. Repeated cycles must never overwrite OPEN/CLOSED state.
    rest.insert_ignore(
        "strategy_forward_evaluations",
        tuple(evaluations),
        on_conflict="evaluation_id",
    )
    rest.insert_ignore(
        "strategy_paper_trades",
        tuple(papers),
        on_conflict="paper_trade_id",
    )
    return len(evaluations), len(source)


def _pending_and_open(rest: ForwardRest) -> tuple[dict[str, object], ...]:
    return rest.select(
        "strategy_paper_trades",
        {
            "select": (
                "paper_trade_id,strategy_id,promotion_stage,symbol,direction,"
                "strategy_timeframe,regime,decision_time_ms,status,entry_time_ms,entry_price,"
                "stop_loss,tp1,tp2,initial_risk,round_trip_cost_bps,mfe_r,mae_r,"
                "tp1_touched,last_observed_ms"
            ),
            "status": "in.(PENDING,OPEN)",
            "order": "decision_time_ms.asc",
            "limit": "1000",
        },
        operation="STRATEGY_FORWARD_OPEN_PAPERS",
    )


def _geometry_valid(direction: str, entry: Decimal, stop: Decimal, tp2: Decimal) -> bool:
    return (direction == "LONG" and stop < entry < tp2) or (
        direction == "SHORT" and tp2 < entry < stop
    )


def _activate(
    rest: ForwardRest,
    market: BinanceDemoPublicRestClient,
    row: dict[str, object],
    *,
    now_ms: int,
) -> dict[str, object] | None:
    decision = int(row["decision_time_ms"])
    if now_ms <= decision:
        return None
    bars = market.get_klines_window(
        str(row["symbol"]),
        EXECUTION_INTERVAL,
        start_time_ms=decision,
        end_time_ms=now_ms + 1,
        max_candles=500,
    )
    entry_bar = next((bar for bar in bars if bar.start_time_ms > decision), None)
    if entry_bar is None:
        return None
    entry = entry_bar.open
    stop = Decimal(str(row["stop_loss"]))
    tp2 = Decimal(str(row["tp2"]))
    if not _geometry_valid(str(row["direction"]), entry, stop, tp2):
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
    patch = {
        "status": "OPEN",
        "entry_time_ms": entry_bar.start_time_ms,
        "entry_price": str(entry),
        "initial_risk": str(risk),
        "mfe_r": "0",
        "mae_r": "0",
        "last_observed_ms": entry_bar.start_time_ms - BAR_MS,
    }
    rest.patch_paper(str(row["paper_trade_id"]), patch)
    return {**row, **patch}


def _advance(
    rest: ForwardRest,
    market: BinanceDemoPublicRestClient,
    row: dict[str, object],
    *,
    now_ms: int,
) -> bool:
    entry_time = int(row["entry_time_ms"])
    entry = Decimal(str(row["entry_price"]))
    stop = Decimal(str(row["stop_loss"]))
    tp1 = _dec(row.get("tp1"))
    tp2 = Decimal(str(row["tp2"]))
    risk = Decimal(str(row["initial_risk"]))
    direction = str(row["direction"])
    last = int(row.get("last_observed_ms") or (entry_time - BAR_MS))
    start = max(entry_time, last + BAR_MS)
    if start >= now_ms:
        return False
    bars = market.get_klines_window(
        str(row["symbol"]),
        EXECUTION_INTERVAL,
        start_time_ms=start,
        end_time_ms=now_ms + 1,
        max_candles=50_000,
    )
    bars = tuple(bar for bar in bars if bar.start_time_ms + BAR_MS <= now_ms)
    if not bars:
        return False

    mfe = Decimal(str(row.get("mfe_r") or 0))
    mae = Decimal(str(row.get("mae_r") or 0))
    tp1_touched = bool(row.get("tp1_touched", False))
    exit_reason: str | None = None
    exit_price: Decimal | None = None
    gross_r: Decimal | None = None
    exit_time: int | None = None
    last_processed = bars[-1].start_time_ms

    for bar in bars:
        if direction == "LONG":
            mfe = max(mfe, max(Decimal(0), bar.high - entry) / risk)
            mae = max(mae, max(Decimal(0), entry - bar.low) / risk)
            tp1_touched = tp1_touched or (tp1 is not None and bar.high >= tp1)
            stop_hit, target_hit = bar.low <= stop, bar.high >= tp2
            if stop_hit:  # conservative same-bar ambiguity policy
                exit_reason, exit_price, gross_r = "STOP_LOSS", stop, Decimal("-1")
            elif target_hit:
                exit_reason, exit_price, gross_r = "TAKE_PROFIT_2", tp2, (tp2 - entry) / risk
        else:
            mfe = max(mfe, max(Decimal(0), entry - bar.low) / risk)
            mae = max(mae, max(Decimal(0), bar.high - entry) / risk)
            tp1_touched = tp1_touched or (tp1 is not None and bar.low <= tp1)
            stop_hit, target_hit = bar.high >= stop, bar.low <= tp2
            if stop_hit:
                exit_reason, exit_price, gross_r = "STOP_LOSS", stop, Decimal("-1")
            elif target_hit:
                exit_reason, exit_price, gross_r = "TAKE_PROFIT_2", tp2, (entry - tp2) / risk
        if exit_reason is not None:
            # Intrabar timestamp is unknown from OHLC. Use completed-bar timestamp,
            # never bar open, so evidence does not claim knowledge before it existed.
            exit_time = bar.start_time_ms + BAR_MS
            last_processed = bar.start_time_ms
            break

    patch: dict[str, object] = {
        "mfe_r": str(mfe),
        "mae_r": str(mae),
        "tp1_touched": tp1_touched,
        "last_observed_ms": last_processed,
    }
    if exit_reason is not None:
        assert exit_price is not None and gross_r is not None and exit_time is not None
        cost_bps = Decimal(str(row.get("round_trip_cost_bps") or BASELINE_ROUND_TRIP_COST_BPS))
        cost_r = entry * cost_bps / Decimal("10000") / risk
        patch.update(
            {
                "status": "CLOSED",
                "exit_time_ms": exit_time,
                "exit_price": str(exit_price),
                "exit_reason": exit_reason,
                "gross_result_r": str(gross_r),
                "net_result_r": str(gross_r - cost_r),
            }
        )
    rest.patch_paper(str(row["paper_trade_id"]), patch)
    return exit_reason is not None


def advance_paper_lifecycle(
    rest: ForwardRest,
    market: BinanceDemoPublicRestClient,
    *,
    now_ms: int,
) -> tuple[int, int, int]:
    opened = closed = failures = 0
    for original in _pending_and_open(rest):
        try:
            row = original
            if str(row["status"]) == "PENDING":
                activated = _activate(rest, market, row, now_ms=now_ms)
                if activated is None:
                    continue
                row = activated
                opened += 1
            if str(row["status"]) == "OPEN" and _advance(rest, market, row, now_ms=now_ms):
                closed += 1
        except Exception as exc:  # isolate a single market/paper failure
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


def _closed_papers(rest: ForwardRest) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for offset in range(0, 10_000, PAGE_SIZE):
        page = rest.select(
            "strategy_paper_trades",
            {
                "select": (
                    "strategy_id,promotion_stage,symbol,direction,strategy_timeframe,regime,"
                    "entry_time_ms,exit_time_ms,net_result_r,mfe_r,mae_r,tp1_touched"
                ),
                "status": "eq.CLOSED",
                "order": "exit_time_ms.asc",
                "limit": str(PAGE_SIZE),
                "offset": str(offset),
            },
            operation="STRATEGY_FORWARD_CLOSED_PAPERS",
        )
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
    return tuple(rows)


def calculate_forward_metrics(rows: tuple[dict[str, object], ...]) -> ForwardMetrics:
    if not rows:
        return ForwardMetrics(
            0,
            0,
            0,
            Decimal(0),
            Decimal(0),
            None,
            Decimal(0),
            Decimal(0),
            Decimal(0),
            Decimal(0),
            0,
        )
    ordered = sorted(rows, key=lambda row: int(row["exit_time_ms"]))
    results = tuple(Decimal(str(row["net_result_r"])) for row in ordered)
    count = Decimal(len(results))
    wins = sum(value > 0 for value in results)
    losses = sum(value < 0 for value in results)
    gross_profit = sum((value for value in results if value > 0), Decimal(0))
    gross_loss = abs(sum((value for value in results if value < 0), Decimal(0)))
    equity = peak = drawdown = Decimal(0)
    for value in results:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    holds = tuple(int(row["exit_time_ms"]) - int(row["entry_time_ms"]) for row in ordered)
    return ForwardMetrics(
        sample_size=len(results),
        wins=wins,
        losses=losses,
        win_rate=Decimal(wins) / count,
        expectancy_r=sum(results, Decimal(0)) / count,
        profit_factor=gross_profit / gross_loss if gross_loss > 0 else None,
        max_drawdown_r=drawdown,
        average_mfe_r=(
            sum((Decimal(str(row.get("mfe_r") or 0)) for row in rows), Decimal(0)) / count
        ),
        average_mae_r=(
            sum((Decimal(str(row.get("mae_r") or 0)) for row in rows), Decimal(0)) / count
        ),
        tp1_touch_rate=Decimal(sum(bool(row.get("tp1_touched")) for row in rows)) / count,
        average_holding_ms=sum(holds) // len(holds),
    )


def _slice(rows: tuple[dict[str, object], ...], keys: tuple[str, ...]) -> dict[str, object]:
    grouped: dict[tuple[str, ...], list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(tuple(str(row.get(key) or "UNKNOWN") for key in keys), []).append(row)
    return {
        "|".join(key): calculate_forward_metrics(tuple(group)).to_dict()
        for key, group in sorted(grouped.items())
    }


def build_forward_summary(
    rows: tuple[dict[str, object], ...],
    *,
    now_ms: int,
    observer_started_at_ms: int,
) -> dict[str, object]:
    return {
        "schema_version": FORWARD_SCHEMA_VERSION,
        "observer_started_at_ms": observer_started_at_ms,
        "generated_at_ms": now_ms,
        "closed_paper_trades": len(rows),
        "entry_policy": "NEXT_5M_OPEN",
        "intrabar_conflict_policy": "SL_FIRST",
        "round_trip_cost_bps": str(BASELINE_ROUND_TRIP_COST_BPS),
        "execution_influence": False,
        "promotion_authority": False,
        "by_strategy": _slice(rows, ("strategy_id",)),
        "by_strategy_symbol": _slice(rows, ("strategy_id", "symbol")),
        "by_strategy_timeframe": _slice(rows, ("strategy_id", "strategy_timeframe")),
        "by_strategy_regime": _slice(rows, ("strategy_id", "regime")),
        "by_strategy_direction": _slice(rows, ("strategy_id", "direction")),
        "by_full_slice": _slice(
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
    with ForwardRest(config) as rest:
        rows: list[dict[str, object]] = []
        for offset in range(0, 10_000, PAGE_SIZE):
            page = rest.select(
                "strategy_paper_trades",
                {
                    "select": "exit_time_ms,net_result_r",
                    "strategy_id": f"eq.{strategy_id}",
                    "promotion_stage": f"eq.{promotion_stage}",
                    "status": "eq.CLOSED",
                    "order": "exit_time_ms.asc",
                    "limit": str(PAGE_SIZE),
                    "offset": str(offset),
                },
                operation="PAPER_FORWARD_PROMOTION_EVIDENCE",
            )
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                break
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
        ForwardRest(config) as rest,
        BinanceDemoPublicRestClient(base_url=runtime.binance_rest_url) as market,
    ):
        observer_started_at_ms = ensure_observer_start(rest, now_ms=timestamp)
        evaluations, source_signals = sync_signal_evaluations(
            rest,
            now_ms=timestamp,
            observer_started_at_ms=observer_started_at_ms,
        )
        opened, closed_now, failures = advance_paper_lifecycle(rest, market, now_ms=timestamp)
        closed_rows = _closed_papers(rest)
        summary = build_forward_summary(
            closed_rows,
            now_ms=timestamp,
            observer_started_at_ms=observer_started_at_ms,
        )
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
                    "status": "SUCCESS" if failures == 0 else "DEGRADED",
                    "git_sha": os.getenv("GITHUB_SHA", "LOCAL"),
                    "details": {
                        "schema_version": FORWARD_SCHEMA_VERSION,
                        "observer_started_at_ms": observer_started_at_ms,
                        "source_strategy_signals": source_signals,
                        "new_evaluations": evaluations,
                        "paper_opened": opened,
                        "paper_closed_now": closed_now,
                        "paper_failures": failures,
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
        "status": (
            "PASS_STRATEGY_FORWARD_EVIDENCE"
            if failures == 0
            else "DEGRADED_STRATEGY_FORWARD_EVIDENCE"
        ),
        "observer_started_at_ms": observer_started_at_ms,
        "source_strategy_signals": source_signals,
        "new_evaluations": evaluations,
        "paper_opened": opened,
        "paper_closed_now": closed_now,
        "paper_failures": failures,
        "closed_sample_total": len(closed_rows),
        "execution_influence": False,
        "promotion_authority": False,
        "live_trading_locked": True,
    }


def main() -> None:
    print(json.dumps(run_forward_evidence_cycle(), indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
