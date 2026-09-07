from __future__ import annotations

from decimal import Decimal

from crypto_scanner.hot_watch import RESEARCH_SCHEMA, RESEARCH_STRATEGY_ID
from crypto_scanner.research_attribution import (
    ResearchTradeSample,
    analyze_factor_attribution,
    build_research_samples,
)


def _telemetry(*, taker: str, schema: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "strategy_id": RESEARCH_STRATEGY_ID,
        "direction": "LONG",
        "discovery_score": "58",
        "score_separation": "7",
        "context_bias": "MIXED",
        "spread_bps": "2",
        "distance_to_discovery_reference_bps": "4",
        "direction_signed_taker_pressure": taker,
        "direction_signed_orderbook_imbalance": "0.1",
        "direction_signed_move_1m_bps": "1",
        "direction_signed_move_3m_bps": "1",
        "direction_signed_funding_rate": "0.0001",
        "one_minute_displacement": False,
        "reclaim_or_retest_1m": False,
        "frames": {
            "15": {
                "alignment_count": 3,
                "adx14": "25",
                "direction_signed_momentum_to_atr": "1.2",
            },
            "60": {
                "alignment_count": 3,
                "adx14": "25",
                "direction_signed_momentum_to_atr": "1.2",
            },
        },
    }
    if schema:
        payload["research_schema"] = RESEARCH_SCHEMA
    return payload


def _sample(index: int, *, taker_aligned: bool, return_bps: str) -> ResearchTradeSample:
    value = "0.5" if taker_aligned else "-0.5"
    return ResearchTradeSample(
        signal_id=f"sig-{index}",
        symbol="BTCUSDT",
        direction="LONG",
        net_pnl=Decimal(return_bps),
        price_return_bps=Decimal(return_bps),
        net_return_bps=Decimal(return_bps),
        mfe_r=Decimal("1.5"),
        mae_r=Decimal("0.4"),
        telemetry=_telemetry(taker=value),
    )


def test_factor_ranking_stays_observe_only_below_20_samples() -> None:
    samples = tuple(_sample(i, taker_aligned=i < 5, return_bps="10") for i in range(10))
    report = analyze_factor_attribution(samples)

    assert report["status"] == "OBSERVE_ONLY"
    assert report["strongest_factor"] is None
    assert report["sample_size"] == 10


def test_taker_flow_can_rank_after_minimum_evidence() -> None:
    samples = tuple(
        _sample(i, taker_aligned=i < 10, return_bps="20" if i < 10 else "-10")
        for i in range(20)
    )
    report = analyze_factor_attribution(samples)

    assert report["status"] == "PRELIMINARY_ATTRIBUTION"
    assert report["strongest_factor"] == "taker_flow_aligned"
    factor = report["factors"]["taker_flow_aligned"]
    assert factor["rank_eligible"] is True
    assert Decimal(factor["delta_net_return_bps"]) == Decimal("30")


def test_build_samples_excludes_pre_instrumentation_trade() -> None:
    closed = [
        {
            "signal_id": "sig-new",
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "entry_qty": "2",
            "average_entry_price": "100",
            "average_exit_price": "101",
            "net_pnl": "1.5",
            "mfe_r": "1.2",
            "mae_r": "0.3",
            "history_complete": True,
            "calibration_eligible": True,
        },
        {
            "signal_id": "sig-legacy",
            "symbol": "ETHUSDT",
            "direction": "LONG",
            "entry_qty": "1",
            "average_entry_price": "100",
            "average_exit_price": "101",
            "net_pnl": "1",
            "history_complete": True,
            "calibration_eligible": True,
        },
    ]
    runtime = [
        {
            "state_key": "hotwatch:run:BTCUSDT:1",
            "updated_at_ms": 100,
            "state": {
                "signal_id": "sig-new",
                "observed_at_ms": 100,
                "telemetry": _telemetry(taker="0.5"),
            },
        },
        {
            "state_key": "hotwatch:run:ETHUSDT:1",
            "updated_at_ms": 100,
            "state": {
                "signal_id": "sig-legacy",
                "observed_at_ms": 100,
                "telemetry": _telemetry(taker="0.5", schema=False),
            },
        },
    ]

    samples = build_research_samples(closed, runtime)

    assert len(samples) == 1
    assert samples[0].signal_id == "sig-new"
    assert samples[0].price_return_bps == Decimal("100")
    assert samples[0].net_return_bps == Decimal("75")
