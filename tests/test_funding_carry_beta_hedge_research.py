from datetime import date

import pytest

import crypto_scanner.funding_carry_beta_hedge_research as module


def test_beta_hedge_reduces_market_beta(monkeypatch: pytest.MonkeyPatch) -> None:
    beta_map = {
        "BTCUSDT": 1.0,
        "ETHUSDT": 1.6,
        "SOLUSDT": 0.8,
    }

    def fake_beta(symbol, signal_day, closes):
        del signal_day, closes
        return beta_map[symbol]

    monkeypatch.setattr(module, "_beta_for_day", fake_beta)
    result = module._beta_hedged_weights(
        {"ETHUSDT": 0.5, "SOLUSDT": -0.5},
        date(2026, 1, 1),
        {},
    )
    assert result is not None
    weights, pre_beta, post_beta = result
    assert pre_beta == pytest.approx(0.4)
    assert abs(post_beta) < 1e-12
    assert sum(abs(value) for value in weights.values()) == pytest.approx(1.0)


def test_beta_hedge_cap_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    beta_map = {
        "BTCUSDT": 1.0,
        "ETHUSDT": 3.0,
    }

    def fake_beta(symbol, signal_day, closes):
        del signal_day, closes
        return beta_map[symbol]

    monkeypatch.setattr(module, "_beta_for_day", fake_beta)
    result = module._beta_hedged_weights(
        {"ETHUSDT": 1.0},
        date(2026, 1, 1),
        {},
    )
    assert result is not None
    weights, pre_beta, post_beta = result
    assert pre_beta == pytest.approx(3.0)
    assert weights["BTCUSDT"] < 0
    assert abs(post_beta) < abs(pre_beta)
    assert sum(abs(value) for value in weights.values()) == pytest.approx(1.0)


def test_v2_contract_is_fixed() -> None:
    assert module.BETA_WINDOW_DAYS == 60
    assert module.MAX_ABS_BTC_HEDGE == 0.50
    assert module.BASE_ROUND_TRIP_BPS == 8.0
    assert module.STRESS_ROUND_TRIP_BPS == 14.0
    assert [candidate.name for candidate in module.FROZEN_V2] == [
        "FCR_BH_14D_TOP3_3DAY_BETA60",
        "FCR_BH_28D_TOP3_WEEKLY_BETA60",
    ]
