from decimal import Decimal

from crypto_scanner.closed_trades import TradeDirection
from crypto_scanner.profit_lock import (
    _safe_locked_r,
    _stop_from_locked_r,
    locked_r_from_mfe,
)
from crypto_scanner.strategy_params import StrategyParameters


def test_default_profit_lock_staircase_is_conservative_and_monotonic() -> None:
    strategy = StrategyParameters()

    assert locked_r_from_mfe(Decimal("0.99"), strategy) is None
    assert locked_r_from_mfe(Decimal("1.00"), strategy) == Decimal("0.05")
    assert locked_r_from_mfe(Decimal("1.49"), strategy) == Decimal("0.05")
    assert locked_r_from_mfe(Decimal("1.50"), strategy) == Decimal("0.50")
    assert locked_r_from_mfe(Decimal("2.00"), strategy) == Decimal("1.00")
    assert locked_r_from_mfe(Decimal("3.00"), strategy) == Decimal("2.00")


def test_late_retrace_clamps_lock_behind_current_mark_instead_of_immediate_trigger() -> None:
    assert _safe_locked_r(
        target_locked_r=Decimal("2.00"),
        current_r=Decimal("1.60"),
    ) == Decimal("1.50")
    assert _safe_locked_r(
        target_locked_r=Decimal("1.00"),
        current_r=Decimal("0.12"),
    ) is None


def test_profit_lock_stop_rounds_less_aggressively_to_exchange_tick() -> None:
    long_stop = _stop_from_locked_r(
        entry_price=Decimal("100.03"),
        initial_stop=Decimal("99.03"),
        direction=TradeDirection.LONG,
        locked_r=Decimal("0.50"),
        tick_size=Decimal("0.10"),
    )
    short_stop = _stop_from_locked_r(
        entry_price=Decimal("100.03"),
        initial_stop=Decimal("101.03"),
        direction=TradeDirection.SHORT,
        locked_r=Decimal("0.50"),
        tick_size=Decimal("0.10"),
    )

    assert long_stop == Decimal("100.50")
    assert short_stop == Decimal("99.60")


def test_calibrated_profit_lock_gap_stays_bounded() -> None:
    StrategyParameters(
        profit_lock_activation_r=Decimal("0.90"),
        profit_lock_gap_r=Decimal("0.75"),
    ).validate()
