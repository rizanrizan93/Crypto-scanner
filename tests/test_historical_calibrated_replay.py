from decimal import Decimal

from crypto_scanner.binance.models import Candle
from crypto_scanner.historical_replay import replay_calibrated_geometry
from crypto_scanner.strategy_params import StrategyParameters


def _candle(index: int, open_: str, high: str, low: str, close: str) -> Candle:
    return Candle(
        index * 60_000,
        Decimal(open_),
        Decimal(high),
        Decimal(low),
        Decimal(close),
        Decimal(1),
        Decimal(100),
    )


def test_profit_lock_only_affects_following_bar() -> None:
    outcome = replay_calibrated_geometry(
        (
            _candle(1, "100", "102.1", "99.5", "102"),
            _candle(2, "102", "102.2", "100.5", "101"),
        ),
        direction="LONG",
        entry_price=Decimal(100),
        stop_loss=Decimal(99),
        take_profit=Decimal(103),
        strategy=StrategyParameters(),
    )
    assert outcome.exit_reason == "PROFIT_LOCK"
    assert outcome.result_r == Decimal(1)


def test_intrabar_initial_stop_remains_first() -> None:
    outcome = replay_calibrated_geometry(
        (_candle(1, "100", "103.5", "98.5", "102"),),
        direction="LONG",
        entry_price=Decimal(100),
        stop_loss=Decimal(99),
        take_profit=Decimal(103),
        strategy=StrategyParameters(),
    )
    assert outcome.exit_reason == "SL"
    assert outcome.result_r == Decimal(-1)
