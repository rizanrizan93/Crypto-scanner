from __future__ import annotations

import pytest

from crypto_scanner.persistence import SupabasePersistenceConfig
from crypto_scanner.post_fill_recovery import _load_recoverable_plan

CURRENT_SIGNAL_ID = "sig-current0123456789abcdef0123456789"


def _config() -> SupabasePersistenceConfig:
    return SupabasePersistenceConfig(
        url="https://abc.supabase.co",
        service_role_key="secret",
    )


def test_recovery_order_query_is_bound_to_current_open_position_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_order_params: list[dict[str, str]] = []

    class FakeRecoveryRestClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def select(self, table: str, *, params: dict[str, str]):
            if table == "positions":
                return [
                    {
                        "position_id": "pos-current",
                        "signal_id": CURRENT_SIGNAL_ID,
                    }
                ]
            if table == "orders":
                observed_order_params.append(dict(params))
                return []
            raise AssertionError(f"unexpected table: {table}")

    monkeypatch.setattr(
        "crypto_scanner.post_fill_recovery._RecoveryRestClient",
        FakeRecoveryRestClient,
    )

    assert _load_recoverable_plan(_config(), "TRXUSDT") is None
    assert len(observed_order_params) == 1
    assert observed_order_params[0]["symbol"] == "eq.TRXUSDT"
    assert observed_order_params[0]["signal_id"] == f"eq.{CURRENT_SIGNAL_ID}"


def test_recovery_without_durable_open_row_keeps_original_symbol_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_order_params: list[dict[str, str]] = []

    class FakeRecoveryRestClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def select(self, table: str, *, params: dict[str, str]):
            if table == "positions":
                return []
            if table == "orders":
                observed_order_params.append(dict(params))
                return []
            raise AssertionError(f"unexpected table: {table}")

    monkeypatch.setattr(
        "crypto_scanner.post_fill_recovery._RecoveryRestClient",
        FakeRecoveryRestClient,
    )

    assert _load_recoverable_plan(_config(), "TRXUSDT") is None
    assert len(observed_order_params) == 1
    assert observed_order_params[0]["symbol"] == "eq.TRXUSDT"
    assert "signal_id" not in observed_order_params[0]
