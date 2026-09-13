from datetime import UTC, datetime
from decimal import Decimal

import pytest

from crypto_scanner.historical_research import HistoricalResearchTrade
from crypto_scanner.research_tournament import (
    MonthKey,
    ResearchCandidate,
    apply_cost,
    build_candidate_report,
    iter_months,
    max_drawdown_r,
    research_score,
    split_trades_by_calendar,
)


def _ms(year: int, month: int, day: int = 15) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


def _trade(
    *,
    year: int,
    month: int,
    result_r: str,
    symbol: str = "BTCUSDT",
    entry: str = "100",
    stop: str = "99",
) -> HistoricalResearchTrade:
    value = Decimal(result_r)
    return HistoricalResearchTrade(
        decision_time_ms=_ms(year, month) - 300_000,
        entry_time_ms=_ms(year, month),
        direction="LONG",
        impulse_index=year * 100 + month,
        entry_price=Decimal(entry),
        stop_loss=Decimal(stop),
        take_profit=Decimal(entry) + Decimal("2"),
        gross_result_r=value,
        net_result_r=value,
        mfe_r=max(value, Decimal(0)),
        mae_r=abs(min(value, Decimal(0))),
        exit_reason="TP" if value > 0 else "SL",
        symbol=symbol,
    )


def _candidate() -> ResearchCandidate:
    return ResearchCandidate(
        name="TEST",
        horizon_bars=20,
        impulse_atr=Decimal("1.2"),
        retest_tolerance_atr=Decimal("0.3"),
        stop_buffer_atr=Decimal("0.15"),
        target_r=Decimal("2"),
    )


def test_iter_months_crosses_year_boundary() -> None:
    rows = iter_months(MonthKey(2025, 11), MonthKey(2026, 2))
    assert [row.label() for row in rows] == [
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
    ]


def test_calendar_split_is_frozen_by_month_not_trade_count() -> None:
    rows = (
        _trade(year=2024, month=2, result_r="1"),
        _trade(year=2025, month=5, result_r="1"),
        _trade(year=2026, month=3, result_r="1"),
    )
    split = split_trades_by_calendar(
        rows,
        start=MonthKey(2024, 1),
        train_through=MonthKey(2024, 12),
        validation_through=MonthKey(2025, 12),
        end=MonthKey(2026, 8),
    )
    assert len(split["train"]) == 1
    assert len(split["validation"]) == 1
    assert len(split["oos"]) == 1


def test_cost_repricing_uses_immutable_geometry() -> None:
    rows = (_trade(year=2026, month=1, result_r="2", entry="100", stop="99"),)
    base = apply_cost(rows, round_trip_cost_bps=Decimal("8"))
    stress = apply_cost(rows, round_trip_cost_bps=Decimal("14"))

    assert base[0].gross_result_r == Decimal("2")
    assert base[0].net_result_r == Decimal("1.92")
    assert stress[0].net_result_r == Decimal("1.86")
    assert stress[0].entry_price == rows[0].entry_price
    assert stress[0].stop_loss == rows[0].stop_loss


def test_max_drawdown_uses_chronological_net_r() -> None:
    rows = (
        _trade(year=2026, month=1, result_r="2"),
        _trade(year=2026, month=2, result_r="-1"),
        _trade(year=2026, month=3, result_r="-2"),
        _trade(year=2026, month=4, result_r="1"),
    )
    assert max_drawdown_r(rows) == Decimal("3")


def test_candidate_report_keeps_oos_and_cost_stress_separate() -> None:
    gross = {
        "BTCUSDT": (
            _trade(year=2024, month=6, result_r="1", symbol="BTCUSDT"),
            _trade(year=2025, month=6, result_r="1", symbol="BTCUSDT"),
            _trade(year=2026, month=6, result_r="1", symbol="BTCUSDT"),
        ),
        "ETHUSDT": (
            _trade(year=2024, month=7, result_r="-1", symbol="ETHUSDT"),
            _trade(year=2025, month=7, result_r="-1", symbol="ETHUSDT"),
            _trade(year=2026, month=7, result_r="-1", symbol="ETHUSDT"),
        ),
    }
    report = build_candidate_report(
        gross,
        candidate=_candidate(),
        start=MonthKey(2024, 1),
        train_through=MonthKey(2024, 12),
        validation_through=MonthKey(2025, 12),
        end=MonthKey(2026, 8),
        base_cost_bps=Decimal("8"),
        stress_cost_bps=Decimal("14"),
    )

    base_oos = report["partitions"]["base"]["periods"]["oos"]
    stress_oos = report["partitions"]["stress"]["periods"]["oos"]
    assert base_oos["summary"]["sample_size"] == 2
    assert stress_oos["summary"]["sample_size"] == 2
    assert Decimal(stress_oos["summary"]["average_net_r"]) < Decimal(
        base_oos["summary"]["average_net_r"]
    )
    assert base_oos["breadth"]["eligible_symbols"] == 2


def test_research_score_ranks_stress_oos_first() -> None:
    base = {
        "partitions": {
            "base": {
                "periods": {
                    "oos": {
                        "summary": {"average_net_r": "0.20"},
                        "breadth": {"positive_fraction": "0.50"},
                    }
                }
            },
            "stress": {
                "periods": {"oos": {"summary": {"average_net_r": "0.05"}}}
            },
        }
    }
    stronger_stress = {
        "partitions": {
            "base": {
                "periods": {
                    "oos": {
                        "summary": {"average_net_r": "0.10"},
                        "breadth": {"positive_fraction": "0.40"},
                    }
                }
            },
            "stress": {
                "periods": {"oos": {"summary": {"average_net_r": "0.06"}}}
            },
        }
    }
    assert research_score(stronger_stress) > research_score(base)


def test_invalid_calendar_split_fails_closed() -> None:
    with pytest.raises(ValueError):
        split_trades_by_calendar(
            (),
            start=MonthKey(2024, 1),
            train_through=MonthKey(2025, 12),
            validation_through=MonthKey(2025, 12),
            end=MonthKey(2026, 8),
        )
