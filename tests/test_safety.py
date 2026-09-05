import pytest

from crypto_scanner.safety import SafetyContract, SafetyError, assert_testnet_url


def test_default_safety_contract_is_valid() -> None:
    SafetyContract().validate()


def test_risk_above_one_percent_fails_closed() -> None:
    with pytest.raises(SafetyError):
        SafetyContract(max_risk_per_trade=0.0101).validate()


def test_excessive_position_count_fails_closed() -> None:
    with pytest.raises(SafetyError):
        SafetyContract(max_concurrent_positions=4).validate()


def test_excessive_leverage_fails_closed() -> None:
    with pytest.raises(SafetyError):
        SafetyContract(max_leverage=4).validate()


def test_live_bybit_rest_endpoint_is_forbidden() -> None:
    with pytest.raises(SafetyError):
        assert_testnet_url("https://api.bybit.com")


def test_live_bybit_websocket_endpoint_is_forbidden() -> None:
    with pytest.raises(SafetyError):
        assert_testnet_url("wss://stream.bybit.com/v5/public/linear")


def test_unknown_endpoint_is_forbidden() -> None:
    with pytest.raises(SafetyError):
        assert_testnet_url("https://example.com")


def test_testnet_endpoints_are_allowed() -> None:
    assert_testnet_url("https://api-testnet.bybit.com")
    assert_testnet_url("wss://stream-testnet.bybit.com/v5/public/linear")
