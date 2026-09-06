import pytest

from crypto_scanner.config import DEFAULT_UNIVERSE, RuntimeConfig, load_runtime_config


def test_default_universe_contains_20_liquid_usdt_pairs() -> None:
    assert DEFAULT_UNIVERSE == (
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "BNBUSDT",
        "DOGEUSDT",
        "ADAUSDT",
        "TRXUSDT",
        "LINKUSDT",
        "AVAXUSDT",
        "SUIUSDT",
        "LTCUSDT",
        "BCHUSDT",
        "DOTUSDT",
        "UNIUSDT",
        "AAVEUSDT",
        "NEARUSDT",
        "ETCUSDT",
        "FILUSDT",
        "ATOMUSDT",
    )
    assert len(DEFAULT_UNIVERSE) == 20


def test_runtime_config_defaults_to_no_supabase() -> None:
    config = RuntimeConfig()
    config.validate()
    assert config.supabase_enabled is False


def test_runtime_config_rejects_duplicate_symbols() -> None:
    with pytest.raises(ValueError):
        RuntimeConfig(universe=("BTCUSDT", "BTCUSDT")).validate()


def test_runtime_config_rejects_non_usdt_symbol() -> None:
    with pytest.raises(ValueError):
        RuntimeConfig(universe=("BTCUSD",)).validate()


def test_environment_can_override_universe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRYPTO_SCANNER_UNIVERSE", "BTCUSDT,ETHUSDT")
    config = load_runtime_config()
    assert config.universe == ("BTCUSDT", "ETHUSDT")
