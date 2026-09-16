from decimal import Decimal

from crypto_scanner.market_models import Candle
from crypto_scanner.strategy_forward_evidence import (
    BAR_MS,
    _activate,
    _advance,
    _recent_signals,
    build_forward_summary,
    calculate_forward_metrics,
)


class FakeRest:
    def __init__(self, rows=()):
        self.rows = tuple(rows)
        self.patches = []
        self.select_calls = []

    def select(self, table, params, *, operation):
        self.select_calls.append((table, params, operation))
        return self.rows

    def patch_paper(self, paper_id, fields):
        self.patches.append((paper_id, fields))


class FakeMarket:
    def __init__(self, bars):
        self.bars = tuple(bars)

    def get_klines_window(self, *_args, **_kwargs):
        return self.bars


def bar(start, open_, high, low, close):
    return Candle(
        start_time_ms=start,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal("1"),
        turnover=Decimal("100"),
    )


def base_paper(**overrides):
    row = {
        "paper_trade_id": "paper-abc",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "decision_time_ms": 1_000_000,
        "status": "PENDING",
        "entry_time_ms": None,
        "entry_price": None,
        "stop_loss": "95",
        "tp1": "105",
        "tp2": "110",
        "initial_risk": None,
        "round_trip_cost_bps": "8",
        "mfe_r": None,
        "mae_r": None,
        "tp1_touched": False,
        "last_observed_ms": None,
    }
    row.update(overrides)
    return row


def test_activation_uses_first_bar_after_decision_not_signal_price():
    decision_bar = bar(1_000_000, 98, 101, 97, 100)
    next_bar = bar(1_300_000, 100, 102, 99, 101)
    rest = FakeRest()

    activated = _activate(
        rest,
        FakeMarket((decision_bar, next_bar)),
        base_paper(),
        now_ms=2_000_000,
    )

    assert activated is not None
    assert activated["entry_time_ms"] == 1_300_000
    assert activated["entry_price"] == "100"
    assert activated["initial_risk"] == "5"
    assert rest.patches[-1][1]["status"] == "OPEN"


def test_gap_that_invalidates_frozen_geometry_is_rejected_not_rewritten():
    rest = FakeRest()
    activated = _activate(
        rest,
        FakeMarket((bar(1_300_000, 94, 96, 93, 95),)),
        base_paper(),
        now_ms=2_000_000,
    )

    assert activated is None
    patch = rest.patches[-1][1]
    assert patch["status"] == "INVALID"
    assert patch["exit_reason"] == "NEXT_BAR_OPEN_INVALIDATED_FROZEN_GEOMETRY"


def test_same_bar_stop_and_target_conflict_is_stop_first_with_costs():
    rest = FakeRest()
    row = base_paper(
        status="OPEN",
        entry_time_ms=1_300_000,
        entry_price="100",
        initial_risk="5",
        last_observed_ms=1_000_000,
        mfe_r="0",
        mae_r="0",
    )
    conflict = bar(1_300_000, 100, 111, 94, 100)

    closed = _advance(rest, FakeMarket((conflict,)), row, now_ms=1_300_000 + BAR_MS)

    assert closed is True
    patch = rest.patches[-1][1]
    assert patch["status"] == "CLOSED"
    assert patch["exit_reason"] == "STOP_LOSS"
    assert Decimal(str(patch["gross_result_r"])) == Decimal("-1")
    assert Decimal(str(patch["net_result_r"])) == Decimal("-1.016")
    assert patch["exit_time_ms"] == 1_300_000 + BAR_MS
    assert patch["tp1_touched"] is True


def test_short_take_profit_is_directionally_symmetric():
    rest = FakeRest()
    row = base_paper(
        direction="SHORT",
        status="OPEN",
        entry_time_ms=1_300_000,
        entry_price="100",
        stop_loss="105",
        tp1="95",
        tp2="90",
        initial_risk="5",
        last_observed_ms=1_000_000,
        mfe_r="0",
        mae_r="0",
    )
    target = bar(1_300_000, 100, 101, 89, 90)

    assert _advance(rest, FakeMarket((target,)), row, now_ms=1_300_000 + BAR_MS)
    patch = rest.patches[-1][1]
    assert patch["exit_reason"] == "TAKE_PROFIT_2"
    assert Decimal(str(patch["gross_result_r"])) == Decimal("2")
    assert Decimal(str(patch["net_result_r"])) == Decimal("1.984")


def test_metrics_and_full_slice_are_strategy_pair_regime_direction_specific():
    rows = (
        {
            "strategy_id": "s1",
            "symbol": "BTCUSDT",
            "strategy_timeframe": "D1",
            "regime": "TREND",
            "direction": "LONG",
            "entry_time_ms": 0,
            "exit_time_ms": 100,
            "net_result_r": "2",
            "mfe_r": "2.5",
            "mae_r": "0.3",
            "tp1_touched": True,
        },
        {
            "strategy_id": "s1",
            "symbol": "BTCUSDT",
            "strategy_timeframe": "D1",
            "regime": "TREND",
            "direction": "LONG",
            "entry_time_ms": 100,
            "exit_time_ms": 300,
            "net_result_r": "-1",
            "mfe_r": "0.2",
            "mae_r": "1.0",
            "tp1_touched": False,
        },
    )
    metrics = calculate_forward_metrics(rows)
    summary = build_forward_summary(rows, now_ms=500, observer_started_at_ms=1)

    assert metrics.sample_size == 2
    assert metrics.win_rate == Decimal("0.5")
    assert metrics.expectancy_r == Decimal("0.5")
    assert metrics.profit_factor == Decimal("2")
    assert metrics.max_drawdown_r == Decimal("1")
    key = "s1|BTCUSDT|D1|TREND|LONG"
    assert summary["by_full_slice"][key]["sample_size"] == 2
    assert summary["observer_started_at_ms"] == 1


def test_recent_signal_query_is_clamped_to_prospective_observer_start():
    rest = FakeRest(())
    _recent_signals(rest, now_ms=10_000_000, floor_ms=9_000_000)

    assert rest.select_calls
    params = rest.select_calls[0][1]
    assert params["created_at_ms"] == "gte.9000000"


def test_empty_metrics_are_defined_without_fake_profit_factor():
    metrics = calculate_forward_metrics(())
    assert metrics.sample_size == 0
    assert metrics.expectancy_r == Decimal(0)
    assert metrics.profit_factor is None