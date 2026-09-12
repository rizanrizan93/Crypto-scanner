from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace

from test_position_manager_write import FakeReader, FakeWriter, _algo

from crypto_scanner import profit_lock_watch
from crypto_scanner.persistence import SupabasePersistenceConfig

CONFIG = SupabasePersistenceConfig("https://example.supabase.co", "test-only")


def _wire_flat_tick(monkeypatch, reader: FakeReader, writer: FakeWriter | None = None) -> None:
    monkeypatch.setenv("CRYPTO_SCANNER_TESTNET_EXECUTION", "ENABLED")
    monkeypatch.setattr(
        profit_lock_watch.BinanceDemoCredentials,
        "from_environment",
        lambda: object(),
    )
    monkeypatch.setattr(
        profit_lock_watch.SupabasePersistenceConfig,
        "from_environment",
        lambda: CONFIG,
    )
    monkeypatch.setattr(
        profit_lock_watch,
        "BinanceDemoPrivateReadOnlyClient",
        lambda *args, **kwargs: nullcontext(reader),
    )
    if writer is not None:
        monkeypatch.setattr(
            profit_lock_watch,
            "BinanceTestnetOrderClient",
            lambda *args, **kwargs: nullcontext(writer),
        )


def test_flat_tick_cleans_scanner_owned_take_profit_orphan(monkeypatch) -> None:
    reader = FakeReader((), [_algo("cs-eth-tp2", "TAKE_PROFIT_MARKET", "5.079", "2534.04")])
    writer = FakeWriter(reader)
    _wire_flat_tick(monkeypatch, reader, writer)

    result = profit_lock_watch.run_profit_lock_tick()

    assert result.status == "PASS_NO_POSITION_ORPHANS_CLEANED"
    assert result.cleaned_orphan_symbols == ("XRPUSDT",)
    assert result.cancelled_orphan_ids == ("cs-eth-tp2",)
    assert result.orphan_cleanup_blockers == ()
    assert writer.cancelled == ["cs-eth-tp2"]
    assert reader.orders == []
    assert result.persistence_status == "NOT_REQUIRED"
    assert result.live_trading_locked


def test_flat_tick_cleans_stop_and_take_profit_siblings(monkeypatch) -> None:
    reader = FakeReader(
        (),
        [
            _algo("cs-flat-sl", "STOP_MARKET", "2", "1.40"),
            _algo("cs-flat-tp", "TAKE_PROFIT_MARKET", "2", "2.00"),
        ],
    )
    writer = FakeWriter(reader)
    _wire_flat_tick(monkeypatch, reader, writer)

    result = profit_lock_watch.run_profit_lock_tick()

    assert result.status == "PASS_NO_POSITION_ORPHANS_CLEANED"
    assert set(result.cancelled_orphan_ids) == {"cs-flat-sl", "cs-flat-tp"}
    assert set(writer.cancelled) == {"cs-flat-sl", "cs-flat-tp"}
    assert reader.orders == []


def test_flat_tick_preserves_manual_conditional_and_never_creates_writer(monkeypatch) -> None:
    manual = replace(_algo("cs-template", "TAKE_PROFIT_MARKET", "1", "2.00"), client_algo_id="manual-tp")
    reader = FakeReader((), [manual])
    _wire_flat_tick(monkeypatch, reader)

    def forbidden(*args, **kwargs):
        raise AssertionError("manual-only flat tick must not initialize exchange writer")

    monkeypatch.setattr(profit_lock_watch, "BinanceTestnetOrderClient", forbidden)

    result = profit_lock_watch.run_profit_lock_tick()

    assert result.status == "PASS_NO_POSITION"
    assert reader.orders == [manual]
    assert result.cancelled_orphan_ids == ()


def test_malformed_scanner_orphan_fails_closed_without_cancel(monkeypatch) -> None:
    malformed = replace(
        _algo("cs-malformed", "TAKE_PROFIT_MARKET", "1", "2.00"),
        reduce_only=False,
    )
    reader = FakeReader((), [malformed])
    writer = FakeWriter(reader)
    _wire_flat_tick(monkeypatch, reader, writer)

    result = profit_lock_watch.run_profit_lock_tick()

    assert result.status == "BLOCKED_FLAT_ORPHAN"
    assert "UNSAFE_FLAT_ORPHAN:XRPUSDT" in result.orphan_cleanup_blockers
    assert writer.cancelled == []
    assert reader.orders == [malformed]
    assert result.degraded_reason == "FLAT_ORPHAN_CLEANUP_BLOCKED"
