from decimal import Decimal

from crypto_scanner.historical_research import HistoricalResearchTrade
from crypto_scanner.run_historical_research import build_report, chronological_split


def _trade(i: int, result_r: str) -> HistoricalResearchTrade:
    return HistoricalResearchTrade(
        decision_time_ms=i * 1_000,
        entry_time_ms=i * 1_000 + 1,
        direction="LONG",
        impulse_index=i,
        entry_price=Decimal("100"),
        stop_loss=Decimal("99"),
        take_profit=Decimal("102"),
        gross_result_r=Decimal(result_r),
        net_result_r=Decimal(result_r),
        mfe_r=max(Decimal(result_r), Decimal(0)),
        mae_r=Decimal("0.5"),
        exit_reason="TP" if Decimal(result_r) > 0 else "SL",
    )


def test_chronological_split_is_60_20_20_and_ordered() -> None:
    trades = tuple(_trade(i, "1") for i in range(10, 0, -1))

    split = chronological_split(trades)

    assert [item.impulse_index for item in split["train"]] == [1, 2, 3, 4, 5, 6]
    assert [item.impulse_index for item in split["validation"]] == [7, 8]
    assert [item.impulse_index for item in split["oos"]] == [9, 10]


def test_build_report_keeps_historical_and_forward_demo_evidence_separate() -> None:
    trades = tuple(_trade(i, "1" if i % 2 else "-1") for i in range(1, 11))

    report = build_report(
        trades,
        symbol="BTCUSDT",
        interval="5m",
        year=2026,
        month=8,
        round_trip_cost_bps=Decimal("8"),
    )

    assert report["research_only"] is True
    assert report["forward_demo_evidence_mutated"] is False
    assert report["live_execution_enabled"] is False
    assert report["sample_count"] == 10
    assert report["partitions"]["train"]["sample_count"] == 6
    assert report["partitions"]["validation"]["sample_count"] == 2
    assert report["partitions"]["oos"]["sample_count"] == 2
