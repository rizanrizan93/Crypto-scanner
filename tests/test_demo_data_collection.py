from decimal import Decimal
from pathlib import Path

from crypto_scanner.demo_data_calibration import demo_calibration_subject
from crypto_scanner.managed_scanner_cycle import authorize_demo_data_collection
from crypto_scanner.strategy_params import StrategyParameters
from crypto_scanner.strategy_promotion import (
    PromotionStage,
    StrategyRuntimeSelection,
    make_strategy_version,
)


def _runtime(stage: str, *, authorized: bool = False) -> StrategyRuntimeSelection:
    return StrategyRuntimeSelection(
        strategy_id="strategy-0123456789abcdef0123",
        params=StrategyParameters(),
        promotion_stage=stage,
        execution_authorized=authorized,
        forward_demo_candidate=False,
    )


def test_historical_rejected_candidate_can_execute_only_for_explicit_demo_collection() -> None:
    rejected = _runtime(PromotionStage.HISTORICAL_REJECTED.value)

    disabled = authorize_demo_data_collection(rejected, enabled=False)
    enabled = authorize_demo_data_collection(rejected, enabled=True)

    assert disabled.execution_authorized is False
    assert enabled.execution_authorized is True
    assert enabled.promotion_stage == PromotionStage.HISTORICAL_REJECTED.value
    assert enabled.forward_demo_candidate is False


def test_quarantined_strategy_remains_fail_closed_in_demo_collection_mode() -> None:
    quarantined = _runtime(PromotionStage.QUARANTINED.value)
    result = authorize_demo_data_collection(quarantined, enabled=True)
    assert result.execution_authorized is False


def test_existing_promotion_authorization_is_unchanged() -> None:
    promoted = _runtime(PromotionStage.PROMOTED.value, authorized=True)
    result = authorize_demo_data_collection(promoted, enabled=True)
    assert result == promoted


def test_demo_calibration_uses_rejected_candidate_only_when_no_champion_exists() -> None:
    candidate = make_strategy_version(
        StrategyParameters(),
        source="TEST",
        created_at_ms=1,
    )
    champion = make_strategy_version(
        StrategyParameters(max_chase_atr=Decimal("0.79")),
        source="TEST_CHAMPION",
        created_at_ms=2,
    )

    assert (
        demo_calibration_subject(
            PromotionStage.HISTORICAL_REJECTED,
            champion=None,
            candidate=candidate,
        )
        == candidate
    )
    assert (
        demo_calibration_subject(
            PromotionStage.HISTORICAL_REJECTED,
            champion=champion,
            candidate=candidate,
        )
        is None
    )
    assert (
        demo_calibration_subject(
            PromotionStage.FORWARD_DEMO,
            champion=None,
            candidate=candidate,
        )
        is None
    )


def test_workflows_explicitly_separate_demo_collection_from_forward_promotion() -> None:
    scanner = Path(".github/workflows/demo-scanner-runtime.yml").read_text()
    calibration = Path(".github/workflows/demo-calibration-runtime.yml").read_text()

    assert "CRYPTO_SCANNER_DEMO_DATA_COLLECTION" in scanner
    assert "HISTORICAL_PENDING/HISTORICAL_REJECTED" in scanner
    assert "FORWARD_DEMO promotion credit" in scanner
    assert "crypto-scanner-calibrate-demo-data" in calibration
    assert "do not receive FORWARD_DEMO credit" in calibration
