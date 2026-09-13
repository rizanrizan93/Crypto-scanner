from crypto_scanner.config import DEFAULT_UNIVERSE, RuntimeConfig


def test_fixed_20_pair_universe_contract():
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
        "XLMUSDT",
        "ATOMUSDT",
    )
    assert len(DEFAULT_UNIVERSE) == 20
    assert "XLMUSDT" in DEFAULT_UNIVERSE
    assert "FILUSDT" not in DEFAULT_UNIVERSE
    RuntimeConfig().validate()
