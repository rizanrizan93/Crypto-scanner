import pytest

from crypto_scanner.safety import (
    SafetyContract,
    SafetyError,
    Venue,
    assert_binance_demo_url,
    assert_testnet_url,
)


def test_default_safety_contract_is_binance_demo_and_valid() -> None:
    contract = SafetyContract()
    contract.validate()
    assert contract.venue is Venue.BINANCE
    assert contract.live_trading_locked is True


def test_risk_above_one_percent_fails_closed() -> None:
    with pytest.raises(SafetyError):
        SafetyContract(max_risk_per_trade=0.0101).validate()


def test_excessive_position_count_fails_closed() -> None:
    with pytest.raises(SafetyError):
        SafetyContract(max_concurrent_positions=4).validate()


def test_excessive_leverage_fails_closed() -> None:
    with pytest.raises(SafetyError):
        SafetyContract(max_leverage=4).validate()


def test_live_binance_rest_endpoint_is_forbidden() -> None:
    with pytest.raises(SafetyError):
        assert_binance_demo_url("https://fapi.binance.com")


def test_binance_demo_endpoint_is_allowed() -> None:
    assert_binance_demo_url("https://demo-fapi.binance.com/fapi/v1/ping")


def test_unknown_binance_endpoint_is_forbidden() -> None:
    with pytest.raises(SafetyError):
        assert_binance_demo_url("https://example.com")


def test_legacy_bybit_testnet_guard_remains_fail_closed() -> None:
    with pytest.raises(SafetyError):
        assert_testnet_url("https://api.bybit.com")
    assert_testnet_url("https://api-testnet.bybit.com")
