from __future__ import annotations

from crypto_scanner import strategy_promotion_runtime as runtime
from crypto_scanner.persistence import SupabasePersistenceConfig
from crypto_scanner.strategy_promotion import PromotionStage


def _config() -> SupabasePersistenceConfig:
    return SupabasePersistenceConfig(
        url="https://example.supabase.co",
        service_role_key="test-only",
    )


def test_strategy_signal_lookup_can_require_forward_demo_entry_stage(monkeypatch) -> None:
    observed: list[dict[str, str]] = []

    def fake_rest_rows(
        _config: SupabasePersistenceConfig,
        table: str,
        params: dict[str, str],
    ) -> tuple[dict[str, object], ...]:
        assert table == "signals"
        observed.append(dict(params))
        return ()

    monkeypatch.setattr(runtime, "_rest_rows", fake_rest_rows)

    result = runtime._strategy_signal_ids(
        _config(),
        "strategy-0123456789abcdef0123",
        promotion_stage=PromotionStage.FORWARD_DEMO.value,
    )

    assert result == ()
    assert observed == [
        {
            "select": "signal_id",
            "evidence->>strategy_id": "eq.strategy-0123456789abcdef0123",
            "order": "created_at_ms.asc",
            "limit": "1000",
            "offset": "0",
            "evidence->>promotion_stage": "eq.FORWARD_DEMO",
        }
    ]


def test_forward_demo_evidence_requests_only_forward_demo_signals(monkeypatch) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_signal_ids(
        _config: SupabasePersistenceConfig,
        strategy_id: str,
        *,
        promotion_stage: str | None = None,
    ) -> tuple[str, ...]:
        calls.append((strategy_id, promotion_stage))
        return ()

    monkeypatch.setattr(runtime, "_strategy_signal_ids", fake_signal_ids)

    results, incidents, evidence = runtime.forward_demo_evidence(
        _config(),
        "strategy-0123456789abcdef0123",
    )

    assert calls == [
        ("strategy-0123456789abcdef0123", PromotionStage.FORWARD_DEMO.value)
    ]
    assert results == ()
    assert incidents == 0
    assert evidence["closed_count"] == 0
    assert evidence["promotion_stage_filter"] == PromotionStage.FORWARD_DEMO.value
