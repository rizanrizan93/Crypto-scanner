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
    assert contract.max_concurrent_positions == 10
    assert contract.max_layers_per_symbol == 3
    assert contract.max_portfolio_risk_fraction == 0.05
    assert contract.max_high_correlation_risk_slots == 2
    assert contract.profitable_stacking_enabled is True
    assert contract.one_position_per_symbol is False


def test_risk_above_one_percent_fails_closed() -> None:
    with pytest.raises(SafetyError):
        SafetyContract(max_risk_per_trade=0.0101).validate()


def test_portfolio_risk_above_five_percent_fails_closed() -> None:
    with pytest.raises(SafetyError):
        SafetyContract(max_portfolio_risk_fraction=0.051).validate()


def test_ten_positions_is_allowed_but_eleven_fails_closed() -> None:
    SafetyContract(max_concurrent_positions=10).validate()
    with pytest.raises(SafetyError):
        SafetyContract(max_concurrent_positions=11).validate()


def test_layer_cap_above_three_fails_closed() -> None:
    with pytest.raises(SafetyError):
        SafetyContract(max_layers_per_symbol=4).validate()


def test_stacking_cannot_coexist_with_legacy_one_position_guard() -> None:
    with pytest.raises(SafetyError):
        SafetyContract(one_position_per_symbol=True).validate()


def test_live_lock_cannot_be_disabled() -> None:
    with pytest.raises(SafetyError):
        SafetyContract(live_trading_locked=False).validate()


def test_excessive_leverage_fails_closed() -> None:
    with pytest.raises(SafetyError):
        SafetyContract(max_leverage=4).validate()


def test_live_binance_rest_endpoint_is_forbidden() -> None:
    with pytest.raises(SafetyError):
        assert_binance_demo_url("https://fapi.binance.com")


def test_binance_futures_testnet_endpoint_is_allowed() -> None:
    assert_binance_demo_url("https://testnet.binancefuture.com/fapi/v1/ping")


def test_region_unstable_demo_host_is_not_runtime_allowlisted() -> None:
    with pytest.raises(SafetyError):
        assert_binance_demo_url("https://demo-fapi.binance.com")


def test_unknown_binance_endpoint_is_forbidden() -> None:
    with pytest.raises(SafetyError):
        assert_binance_demo_url("https://example.com")


def test_legacy_bybit_testnet_guard_remains_fail_closed() -> None:
    with pytest.raises(SafetyError):
        assert_testnet_url("https://api.bybit.com")
    assert_testnet_url("https://api-testnet.bybit.com")
