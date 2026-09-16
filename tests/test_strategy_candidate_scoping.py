from types import SimpleNamespace

from crypto_scanner.regime_specialist_cycle import _active_strategy_symbols
from crypto_scanner.regime_specialist_cycle import (
    _scope_discovery_to_active_symbols as scope_regime,
)
from crypto_scanner.regime_specialist_demo import GlobalRegime, RegimeSpecialistDecision
from crypto_scanner.volatility_breakout_cycle import (
    _scope_discovery_to_active_symbols as scope_breakout,
)


def _rows(*symbols: str):
    return tuple(SimpleNamespace(symbol=symbol) for symbol in symbols)


def test_breakout_scope_keeps_active_leg_even_when_global_rank_would_be_later():
    rows = _rows("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT")

    scoped = scope_breakout(rows, frozenset({"BNBUSDT"}))

    assert [row.symbol for row in scoped] == ["BNBUSDT"]


def test_regime_bull_inactive_has_no_execution_scope():
    decision = RegimeSpecialistDecision(
        regime=GlobalRegime.BULL,
        as_of_ms=1,
        bull_switch_active=False,
        bear_short_symbols=(),
    )

    assert _active_strategy_symbols(decision) == frozenset()
    assert scope_regime(_rows("BTCUSDT", "ETHUSDT"), frozenset()) == ()


def test_regime_bear_scopes_only_bottom_three_symbols():
    decision = RegimeSpecialistDecision(
        regime=GlobalRegime.BEAR,
        as_of_ms=1,
        bull_switch_active=False,
        bear_short_symbols=("DOGEUSDT", "ADAUSDT", "SOLUSDT"),
    )
    active = _active_strategy_symbols(decision)
    scoped = scope_regime(
        _rows("BTCUSDT", "DOGEUSDT", "ADAUSDT", "SOLUSDT", "BNBUSDT"),
        active,
    )

    assert active == frozenset({"DOGEUSDT", "ADAUSDT", "SOLUSDT"})
    assert [row.symbol for row in scoped] == ["DOGEUSDT", "ADAUSDT", "SOLUSDT"]
