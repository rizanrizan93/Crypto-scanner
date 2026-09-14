from __future__ import annotations

from decimal import Decimal

from crypto_scanner.market_models import Candle
from crypto_scanner.regime_specialist_demo import DAY_MS
from crypto_scanner.volatility_breakout_demo import (
    SLEEVE_STOP_RISK_BUDGET,
    build_volatility_breakout_decision,
    replay_vol30_state,
)


def _candle(index: int, close: str) -> Candle:
    value = Decimal(close)
    return Candle(
        start_time_ms=index * DAY_MS,
        open=value,
        high=value + Decimal("0.02"),
        low=value - Decimal("0.02"),
        close=value,
        volume=Decimal("1"),
        turnover=Decimal("1"),
    )


def _breakout_history(final_close: str) -> tuple[Candle, ...]:
    # Keep substantial historical volatility inside the 120-day denominator,
    # then compress the latest 30-day channel before the final breakout.
    closes = ["95" if index % 2 == 0 else "105" for index in range(209)]
    closes.extend("99.95" if index % 2 == 0 else "100.05" for index in range(30))
    closes.append(final_close)
    return tuple(_candle(index, close) for index, close in enumerate(closes))


def test_replay_vol30_state_requires_sufficient_history() -> None:
    rows = tuple(_candle(index, "100") for index in range(100))
    assert replay_vol30_state(rows) == 0


def test_replay_vol30_state_can_enter_long() -> None:
    assert replay_vol30_state(_breakout_history("100.30")) == 1


def test_replay_vol30_state_can_enter_short() -> None:
    assert replay_vol30_state(_breakout_history("99.70")) == -1


class _FakeDailySource:
    def __init__(self, rows: dict[str, tuple[Candle, ...]]) -> None:
        self._rows = rows

    def get_klines(self, symbol: str, interval: str, *, limit: int = 200) -> tuple[Candle, ...]:
        assert interval == "D"
        return self._rows[symbol][-limit:]


def test_decision_preserves_bounded_breakout_sleeve_risk() -> None:
    long_rows = _breakout_history("100.30")
    short_rows = _breakout_history("99.70")
    source = _FakeDailySource({"AAAUSDT": long_rows, "BBBUSDT": short_rows})
    now_ms = max(long_rows[-1].start_time_ms, short_rows[-1].start_time_ms) + DAY_MS

    decision = build_volatility_breakout_decision(
        source,
        ("AAAUSDT", "BBBUSDT"),
        now_ms=now_ms,
    )

    assert decision.legs
    assert Decimal(0) < decision.vol_scale <= Decimal(1)
    assert sum((leg.risk_fraction for leg in decision.legs), Decimal(0)) <= SLEEVE_STOP_RISK_BUDGET
    assert {leg.symbol for leg in decision.legs} == {"AAAUSDT", "BBBUSDT"}
