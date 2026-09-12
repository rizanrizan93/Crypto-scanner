"""Permanent regression for Demo runtime incident 34621592918 (cycle 6/12)."""

from contextlib import nullcontext
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from test_position_manager_write import FakeReader, FakeWriter, _algo, _position

from crypto_scanner import management_health, profit_lock, profit_lock_watch
from crypto_scanner.persistence import (
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseRestClient,
    TransientPersistenceError,
    read_deadline,
)
from crypto_scanner.strategy_params import StrategyParameters, load_strategy_parameters
from crypto_scanner.strategy_promotion import strategy_version_id
from crypto_scanner.trade_linkage import DurableTradeLinkage, stable_position_id_from_episode

CONFIG = SupabasePersistenceConfig("https://rlrfnkckqxkinzgawpql.supabase.co", "test-only")
A = StrategyParameters(profit_lock_gap_r=Decimal("0.75"))
B = StrategyParameters(profit_lock_gap_r=Decimal("1.25"))


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr("crypto_scanner.persistence.time.sleep", lambda _: None)
    monkeypatch.setattr(management_health, "_memory_count", 0)
    monkeypatch.delenv("RUNNER_TEMP", raising=False)


@pytest.mark.parametrize(
    "failure",
    [
        408,
        425,
        429,
        500,
        502,
        503,
        504,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.ConnectError,
        httpx.RemoteProtocolError,
    ],
)
def test_strategy_read_recovers(failure, capsys):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            if isinstance(failure, int):
                return httpx.Response(failure)
            raise failure("test transient")
        return httpx.Response(200, json=[{"state": A.to_dict()}])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert load_strategy_parameters(CONFIG, client=client) == A
    assert len(calls) == 2
    logs = capsys.readouterr().out
    assert '"recovered": true' in logs
    assert "test-only" not in logs


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_errors_never_retry(status):
    calls = []
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(status)
            )
        ) as client,
        pytest.raises(PersistenceError) as error,
    ):
        load_strategy_parameters(CONFIG, client=client)
    assert not isinstance(error.value, TransientPersistenceError)
    assert len(calls) == 1


def test_repeated_504_has_typed_exhaustion():
    calls = []
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(504)
            )
        ) as client,
        pytest.raises(TransientPersistenceError) as error,
    ):
        load_strategy_parameters(CONFIG, client=client)
    assert error.value.attempts == 3
    assert len(calls) == 3


@pytest.mark.parametrize(
    "body", [b"not json", b"{}", b'[{"state": 42}]', b'[{"state": {"profit_lock_gap_r": "999"}}]']
)
def test_invalid_success_never_transient_fallback(body):
    calls = []
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(200, content=body)
            )
        ) as client,
        pytest.raises((ValueError, PersistenceError)) as error,
    ):
        load_strategy_parameters(CONFIG, client=client)
    assert not isinstance(error.value, TransientPersistenceError)
    assert len(calls) == 1


def test_writes_not_retried():
    calls = []
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(504)
            )
        ) as client,
        pytest.raises(PersistenceError),
    ):
        SupabaseRestClient(CONFIG, client=client).upsert(
            "signals", ({"signal_id": "x"},), on_conflict=("signal_id",)
        )
    assert len(calls) == 1 and calls[0].method == "POST"


def test_deadline_interrupts_slow_read():
    import time

    start = time.monotonic()
    with pytest.raises(TransientPersistenceError), read_deadline("TEST", seconds=0.02):
        # Busy loop also models a response body trickling below socket timeout.
        while time.monotonic() - start < 1:
            pass
    assert time.monotonic() - start < 0.2


def test_flat_incident_cycle_6_does_not_read_supabase_or_create_writer(monkeypatch):
    monkeypatch.setenv("CRYPTO_SCANNER_TESTNET_EXECUTION", "ENABLED")
    monkeypatch.setattr(
        profit_lock_watch.BinanceDemoCredentials, "from_environment", lambda: object()
    )
    monkeypatch.setattr(
        profit_lock_watch.SupabasePersistenceConfig, "from_environment", lambda: CONFIG
    )
    monkeypatch.setattr(
        profit_lock_watch,
        "BinanceDemoPrivateReadOnlyClient",
        lambda *a, **kw: nullcontext(SimpleNamespace(get_positions=lambda: ())),
    )

    def forbidden(*a, **kw):
        raise AssertionError("flat tick must not read persistence or initialize exchange writer")

    monkeypatch.setattr(profit_lock_watch, "DurableTradeLinkage", forbidden)
    monkeypatch.setattr(profit_lock_watch, "BinanceTestnetOrderClient", forbidden)
    for _cycle in range(12):
        result = profit_lock_watch.run_profit_lock_tick()
        assert result.status == "PASS_NO_POSITION"
        assert result.strategy_source == "NOT_REQUIRED_FLAT"
        assert result.decision_count == 0
        assert result.live_trading_locked


def linked_client(failures, snapshot=None):
    attempts = []
    position_id = stable_position_id_from_episode("XRPUSDT", "LONG", 1000)

    def handler(request):
        attempts.append(request.url.path)
        if failures:
            status = failures.pop(0)
            if status:
                return httpx.Response(status)
        if request.url.path.endswith("positions"):
            payload = [
                {"position_id": position_id, "signal_id": "sig-a", "initial_stop_loss": "1.40"}
            ]
        elif request.url.path.endswith("signals"):
            payload = [
                {
                    "signal_id": "sig-a",
                    "setup": "HL_PULLBACK",
                    "regime": "TREND",
                    "status": "EXECUTION_READY",
                    "evidence": snapshot
                    or {"strategy_id": strategy_version_id(A), "strategy_params": A.to_dict()},
                }
            ]
        elif request.url.path.endswith("signal_geometry"):
            payload = [{"signal_id": "sig-a", "stop_loss": "1.40"}]
        else:
            raise AssertionError("global strategy must never be loaded for position management")
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler)), attempts


def open_fixture(monkeypatch):
    reader = FakeReader(
        (replace(_position(), mark_price=Decimal("1.85")),),
        [
            _algo("cs-sl-old", "STOP_MARKET", "2", "1.40"),
            _algo("cs-tp-old", "TAKE_PROFIT_MARKET", "2", "2.00"),
        ],
    )
    reader.get_user_trades = lambda *a, **kw: ()
    writer = FakeWriter(reader)
    episode = SimpleNamespace(
        direction=profit_lock.TradeDirection.LONG,
        entry_time_ms=1000,
        layered_entry=False,
        entry_price=Decimal("1.50"),
    )
    monkeypatch.setattr(profit_lock, "infer_open_episode", lambda *a: episode)
    monkeypatch.setattr(
        profit_lock,
        "reconstruct_conservative_trajectory",
        lambda *a, **kw: SimpleNamespace(
            history_complete=True, mfe_r=Decimal("2.80"), current_pnl_per_unit=Decimal("0.35")
        ),
    )
    public = SimpleNamespace(
        get_klines_window=lambda *a, **kw: (),
        get_instrument=lambda *a: SimpleNamespace(tick_size=Decimal("0.01")),
    )
    return reader, writer, public


def test_open_504_recovers_and_uses_entry_a_not_current_b(monkeypatch):
    reader, writer, public = open_fixture(monkeypatch)
    with linked_client([504])[0] as client:
        linkage = DurableTradeLinkage(CONFIG, client=client)
        decisions = profit_lock.run_profit_lock(reader, public, writer, linkage, B, now_ms=2000)
    assert decisions[0].status == profit_lock.ProfitLockStatus.RATCHETED
    assert decisions[0].locked_r == Decimal("2.00")  # B would lock only 1.50R
    assert decisions[0].desired_stop > decisions[0].previous_stop
    assert decisions[0].tp2_trigger == Decimal("2.00")
    assert writer.submitted


def test_open_incident_preserves_protection_and_next_tick_recovers(monkeypatch):
    reader, writer, public = open_fixture(monkeypatch)
    with linked_client([])[0] as client:
        linkage = DurableTradeLinkage(CONFIG, client=client)
        profit_lock.run_profit_lock(reader, public, writer, linkage, now_ms=2000)
    previous_orders = list(reader.orders)
    previous_writes = (list(writer.submitted), list(writer.cancelled))
    with linked_client([504, 504, 504])[0] as client:
        decisions = profit_lock.run_profit_lock(
            reader, public, writer, DurableTradeLinkage(CONFIG, client=client), now_ms=2000
        )
    assert decisions[0].status == profit_lock.ProfitLockStatus.DEGRADED_PERSISTENCE_TRANSIENT
    assert reader.orders == previous_orders
    assert (writer.submitted, writer.cancelled) == previous_writes
    with linked_client([])[0] as client:
        result = profit_lock.run_profit_lock(
            reader, public, writer, DurableTradeLinkage(CONFIG, client=client), now_ms=2000
        )
    assert result[0].status == profit_lock.ProfitLockStatus.NO_ACTION_ALREADY_TIGHTER


@pytest.mark.parametrize(
    "evidence",
    [
        {"strategy_id": "wrong", "strategy_params": A.to_dict()},
        {"strategy_id": strategy_version_id(A), "strategy_params": {}},
        {
            "strategy_id": strategy_version_id(A),
            "strategy_params": {**A.to_dict(), "profit_lock_gap_r": "9"},
        },
    ],
)
def test_bad_entry_snapshot_fails_closed_without_writes(monkeypatch, evidence):
    reader, writer, public = open_fixture(monkeypatch)
    with linked_client([], evidence)[0] as client, pytest.raises(profit_lock.ProfitLockError):
        profit_lock.run_profit_lock(
            reader, public, writer, DurableTradeLinkage(CONFIG, client=client)
        )
    assert writer.submitted == writer.cancelled == []


def test_circuit_survives_process_memory_reset_and_recovers(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    for _ in range(3):
        result = management_health.record_tick(degraded=True)
        monkeypatch.setattr(management_health, "_memory_count", 0)
    assert result["sustained_outage"] and result["new_execution_blocked"]
    assert management_health.failure_count() == 3
    assert management_health.record_tick(degraded=False)["status"] == "RECOVERED"
    assert management_health.failure_count() == 0


def test_managed_clock_continues_after_degraded_tick(monkeypatch, capsys):
    from crypto_scanner.managed_scanner_cycle import ProfitLockManagedClock
    from crypto_scanner.profit_lock_watch import ProfitLockWatchResult, emit_profit_lock_tick

    outcomes = iter([False] * 5 + [True] + [False] * 6)

    def tick():
        degraded = next(outcomes)
        return ProfitLockWatchResult(
            "DEGRADED_PERSISTENCE_TRANSIENT" if degraded else "PASS_PROFIT_LOCK_WATCH",
            "BINANCE",
            "DEMO",
            True,
            0,
            (),
            (),
        )

    clock = ProfitLockManagedClock(
        sleep_fn=lambda _: None, tick_fn=tick, emit_fn=emit_profit_lock_tick
    )
    for _ in range(12):
        clock.sleep(60)
    output = capsys.readouterr().out
    assert output.count('"profit_lock_watch"') == 12
    assert '"status": "DEGRADED"' in output
    assert '"status": "RECOVERED"' in output


def test_managed_cycle_only_handles_typed_transient(monkeypatch, capsys):
    from crypto_scanner import managed_scanner_cycle

    def transient():
        raise TransientPersistenceError("STRATEGY_STATE", 3)

    monkeypatch.setattr(managed_scanner_cycle.scanner_cycle, "main", transient)
    managed_scanner_cycle.main()
    assert '"execution_authorized": false' in capsys.readouterr().out

    def invariant():
        raise PersistenceError("invalid schema")

    monkeypatch.setattr(managed_scanner_cycle.scanner_cycle, "main", invariant)
    with pytest.raises(PersistenceError):
        managed_scanner_cycle.main()


def test_new_entry_unavailable_strategy_fails_before_exchange_client(monkeypatch):
    from crypto_scanner import scanner_cycle

    monkeypatch.setattr(scanner_cycle.SupabasePersistenceConfig, "from_environment", lambda: CONFIG)

    def unavailable(*a, **kw):
        raise TransientPersistenceError("STRATEGY_STATE", 3)

    def forbidden(*a, **kw):
        raise AssertionError("must not initialize exchange writer")

    monkeypatch.setattr(scanner_cycle, "load_strategy_runtime", unavailable)
    monkeypatch.setattr(scanner_cycle, "BinanceTestnetOrderClient", forbidden)
    with pytest.raises(TransientPersistenceError):
        scanner_cycle.run_scanner_cycle()


def test_serial_workflow_has_no_profit_lock_error_suppression():
    from pathlib import Path

    text = Path(".github/workflows/demo-scanner-runtime.yml").read_text()
    assert "group: crypto-scanner-demo-runtime-scanner" in text
    assert "cancel-in-progress: false" in text
    assert "crypto-scanner-profit-lock-watch ||" not in text
    assert "crypto-scanner-profit-lock-watch &" not in text
    assert "Disarmed Phase 6 post-audit" in text
