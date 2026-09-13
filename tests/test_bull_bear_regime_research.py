from crypto_scanner.run_bull_bear_regime_research import _ema, _regimes


def test_ema_is_causal_and_same_length() -> None:
    values = [float(i) for i in range(1, 301)]
    ema = _ema(values, 200)
    assert len(ema) == len(values)
    assert ema[0] == values[0]
    assert ema[-1] < values[-1]


def test_bull_and_bear_are_symmetric() -> None:
    rising = [100.0 + i for i in range(300)]
    falling = [500.0 - i for i in range(300)]
    rising_state = _regimes(rising)
    falling_state = _regimes(falling)
    assert rising_state[-1] == 1
    assert falling_state[-1] == -1


def test_conflicting_regime_is_flat() -> None:
    values = [100.0 + i * 0.1 for i in range(240)]
    values.extend([values[-1] - 0.6 * i for i in range(1, 61)])
    state = _regimes(values)
    assert state[-1] in (-1, 0, 1)
    if values[-1] > _ema(values, 200)[-1] and values[-1] / values[-61] - 1.0 < 0:
        assert state[-1] == 0
