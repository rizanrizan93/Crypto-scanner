from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from math import sqrt
from statistics import mean, pstdev

from crypto_scanner.funding_carry_research import (
    FIXED_UNIVERSE,
    FundingPoint,
    PortfolioDay,
    _block_bootstrap_positive_fraction,
    _renormalize_neutral,
    _stress_funding,
    _strict_intraday_funding_sum,
    _turnover,
    _weights,
    candles_to_complete_utc_days,
    split_calendar,
)
from crypto_scanner.run_funding_carry_research import (
    fetch_funding_history,
    fetch_price_history,
)

BASE_ROUND_TRIP_BPS = 8.0
STRESS_ROUND_TRIP_BPS = 14.0
SEVERE_ROUND_TRIP_BPS = 25.0
MAX_SYMBOL_WORKERS = 5
BTC = "BTCUSDT"


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    family: str
    rebalance_days: int = 7


FROZEN_CANDIDATES = (
    Candidate("TSMOM_60D_EMA200_LONG_SHORT_WEEKLY", "TIME_SERIES_TREND"),
    Candidate("BTC_RESID_MOM_14D_TOP3_WEEKLY_BETA60", "RESIDUAL_MOMENTUM"),
    Candidate("BTC_RESID_REVERT_14D_TOP3_WEEKLY_BETA60", "RESIDUAL_MEAN_REVERSION"),
    Candidate("CROWDED_FUNDING_REV_7D_TOP3_WEEKLY", "FUNDING_CROWDING_REVERSAL"),
)


def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1.0 - alpha) * output[-1])
    return output


def _daily_maps(daily_by_symbol):
    closes = {
        symbol: {row.day: row.close for row in rows}
        for symbol, rows in daily_by_symbol.items()
    }
    opens = {
        symbol: {row.day: row.open for row in rows}
        for symbol, rows in daily_by_symbol.items()
    }
    ordered = {
        symbol: sorted(closes[symbol])
        for symbol in FIXED_UNIVERSE
    }
    index = {
        symbol: {day: idx for idx, day in enumerate(ordered[symbol])}
        for symbol in FIXED_UNIVERSE
    }
    ema200 = {}
    for symbol in FIXED_UNIVERSE:
        vals = [closes[symbol][day] for day in ordered[symbol]]
        series = _ema(vals, 200)
        ema200[symbol] = {day: series[idx] for idx, day in enumerate(ordered[symbol])}
    return closes, opens, ordered, index, ema200


def _ret(closes, ordered, index, symbol: str, day: date, lookback: int):
    idx = index[symbol].get(day)
    if idx is None or idx < lookback:
        return None
    start = ordered[symbol][idx - lookback]
    p0 = closes[symbol][start]
    p1 = closes[symbol][day]
    if p0 <= 0:
        return None
    return p1 / p0 - 1.0


def _daily_returns(closes, ordered, symbol: str):
    out = {}
    days = ordered[symbol]
    for idx in range(1, len(days)):
        p0 = closes[symbol][days[idx - 1]]
        p1 = closes[symbol][days[idx]]
        if p0 > 0:
            out[days[idx]] = p1 / p0 - 1.0
    return out


def _beta_to_btc(returns, symbol: str, day: date, window: int = 60):
    if symbol == BTC:
        return 1.0
    common = sorted(set(returns[symbol]) & set(returns[BTC]))
    common = [d for d in common if d <= day]
    if len(common) < window:
        return None
    sample = common[-window:]
    x = [returns[BTC][d] for d in sample]
    y = [returns[symbol][d] for d in sample]
    mx = mean(x)
    my = mean(y)
    var = sum((v - mx) ** 2 for v in x)
    if var <= 0:
        return None
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=False))
    return cov / var


def _residual_score(returns, symbol: str, day: date, lookback: int = 14):
    beta = _beta_to_btc(returns, symbol, day, 60)
    if beta is None:
        return None
    common = sorted(set(returns[symbol]) & set(returns[BTC]))
    common = [d for d in common if d <= day]
    if len(common) < lookback:
        return None
    sample = common[-lookback:]
    return sum(returns[symbol][d] - beta * returns[BTC][d] for d in sample)


def _funding_7d(points: tuple[FundingPoint, ...], day: date):
    start = day - timedelta(days=6)
    selected = [p.funding_rate for p in points if start <= p.timestamp.date() <= day]
    if len(selected) < 14:
        return None
    return sum(selected) / 7.0


def _zscore_map(values: dict[str, float]):
    if len(values) < 2:
        return {k: 0.0 for k in values}
    sigma = pstdev(values.values())
    mu = mean(values.values())
    if sigma <= 0:
        return {k: 0.0 for k in values}
    return {k: (v - mu) / sigma for k, v in values.items()}


def _select_tsmom(signal_day, closes, ordered, index, ema200):
    longs = []
    shorts = []
    for symbol in FIXED_UNIVERSE:
        mom = _ret(closes, ordered, index, symbol, signal_day, 60)
        if mom is None or signal_day not in ema200[symbol]:
            continue
        idx = index[symbol].get(signal_day)
        if idx is None or idx < 199:
            continue
        close = closes[symbol][signal_day]
        trend = ema200[symbol][signal_day]
        if mom > 0 and close > trend:
            longs.append(symbol)
        elif mom < 0 and close < trend:
            shorts.append(symbol)
    return tuple(sorted(longs)), tuple(sorted(shorts))


def _select_residual(signal_day, returns, *, reverse: bool):
    scored = []
    for symbol in FIXED_UNIVERSE:
        if symbol == BTC:
            continue
        score = _residual_score(returns, symbol, signal_day, 14)
        if score is not None:
            scored.append((score, symbol))
    if len(scored) < 6:
        return (), ()
    scored.sort(key=lambda item: (item[0], item[1]))
    low = tuple(symbol for _, symbol in scored[:3])
    high = tuple(symbol for _, symbol in scored[-3:])
    return (low, high) if reverse else (high, low)


def _select_crowding(signal_day, funding_by_symbol, closes, ordered, index):
    fund = {}
    price = {}
    for symbol in FIXED_UNIVERSE:
        f = _funding_7d(funding_by_symbol.get(symbol, ()), signal_day)
        r = _ret(closes, ordered, index, symbol, signal_day, 7)
        if f is not None and r is not None:
            fund[symbol] = f
            price[symbol] = r
    if len(fund) < 8:
        return (), ()
    zf = _zscore_map(fund)
    zp = _zscore_map(price)
    scores = []
    for symbol in sorted(set(zf) & set(zp)):
        if zf[symbol] * zp[symbol] <= 0:
            continue
        score = (1.0 if zp[symbol] > 0 else -1.0) * abs(zf[symbol] * zp[symbol])
        scores.append((score, symbol))
    neg = sorted((x for x in scores if x[0] < 0), key=lambda x: (x[0], x[1]))
    pos = sorted((x for x in scores if x[0] > 0), key=lambda x: (x[0], x[1]))
    if len(neg) < 3 or len(pos) < 3:
        return (), ()
    longs = tuple(symbol for _, symbol in neg[:3])
    shorts = tuple(symbol for _, symbol in pos[-3:])
    return longs, shorts


def _apply_beta_hedge(weights, returns, signal_day: date):
    if not weights:
        return {}
    beta = 0.0
    for symbol, weight in weights.items():
        b = _beta_to_btc(returns, symbol, signal_day, 60)
        if b is None:
            return weights
        beta += weight * b
    hedge = max(-0.50, min(0.50, -beta))
    out = dict(weights)
    out[BTC] = out.get(BTC, 0.0) + hedge
    gross = sum(abs(v) for v in out.values())
    if gross <= 0:
        return {}
    return {k: v / gross for k, v in out.items() if abs(v) > 1e-12}


def _select(candidate, signal_day, closes, ordered, index, ema200, returns, funding):
    if candidate.family == "TIME_SERIES_TREND":
        longs, shorts = _select_tsmom(signal_day, closes, ordered, index, ema200)
        return _weights(longs, shorts)
    if candidate.family == "RESIDUAL_MOMENTUM":
        longs, shorts = _select_residual(signal_day, returns, reverse=False)
        return _apply_beta_hedge(_weights(longs, shorts), returns, signal_day)
    if candidate.family == "RESIDUAL_MEAN_REVERSION":
        longs, shorts = _select_residual(signal_day, returns, reverse=True)
        return _apply_beta_hedge(_weights(longs, shorts), returns, signal_day)
    if candidate.family == "FUNDING_CROWDING_REVERSAL":
        longs, shorts = _select_crowding(signal_day, funding, closes, ordered, index)
        return _weights(longs, shorts)
    raise ValueError(candidate.family)


def _simulate(candidate, daily_by_symbol, funding_by_symbol):
    closes, opens, ordered, index, ema200 = _daily_maps(daily_by_symbol)
    returns = {symbol: _daily_returns(closes, ordered, symbol) for symbol in FIXED_UNIVERSE}
    calendar = sorted({d for symbol in FIXED_UNIVERSE for d in ordered[symbol]})
    previous = {}
    last_rebalance = None
    out = []
    for idx in range(1, len(calendar) - 1):
        signal_day = calendar[idx - 1]
        entry_day = calendar[idx]
        exit_day = calendar[idx + 1]
        rebalance = last_rebalance is None or idx - last_rebalance >= candidate.rebalance_days
        if rebalance:
            target = _select(
                candidate,
                signal_day,
                closes,
                ordered,
                index,
                ema200,
                returns,
                funding_by_symbol,
            )
            last_rebalance = idx
        else:
            target = previous
        tradable = {
            s: w
            for s, w in target.items()
            if entry_day in opens.get(s, {}) and exit_day in opens.get(s, {})
        }
        if candidate.family in {"TIME_SERIES_TREND", "FUNDING_CROWDING_REVERSAL"}:
            weights = _renormalize_neutral(tradable)
        else:
            gross = sum(abs(v) for v in tradable.values())
            weights = {} if gross <= 0 else {s: w / gross for s, w in tradable.items()}
        turnover = _turnover(previous, weights) if rebalance else 0.0
        price_return = 0.0
        funding_return = 0.0
        stress_funding = 0.0
        for symbol, weight in weights.items():
            entry = opens[symbol][entry_day]
            exit_price = opens[symbol][exit_day]
            if entry <= 0:
                continue
            price_return += weight * (exit_price / entry - 1.0)
            fsum = _strict_intraday_funding_sum(
                funding_by_symbol.get(symbol, ()), entry_day=entry_day, exit_day=exit_day
            )
            fpnl = -weight * fsum
            funding_return += fpnl
            stress_funding += _stress_funding(fpnl)
        base_cost = turnover * (BASE_ROUND_TRIP_BPS / 2.0) / 10_000.0
        stress_cost = turnover * (STRESS_ROUND_TRIP_BPS / 2.0) / 10_000.0
        out.append(
            PortfolioDay(
                day=entry_day,
                price_return=price_return,
                funding_return=funding_return,
                base_return=price_return + funding_return - base_cost,
                stress_return=price_return + stress_funding - stress_cost,
                turnover=turnover,
                gross_exposure=sum(abs(v) for v in weights.values()),
                long_symbols=tuple(sorted(s for s, w in weights.items() if w > 0)),
                short_symbols=tuple(sorted(s for s, w in weights.items() if w < 0)),
            )
        )
        previous = weights
    return tuple(out)


def _summary(rows, field: str, extra_cost_bps: float = 0.0):
    values = []
    for row in rows:
        value = float(getattr(row, field))
        if extra_cost_bps:
            value -= row.turnover * (extra_cost_bps / 2.0) / 10_000.0
        values.append(value)
    if not values:
        return {"days": 0, "total_return": 0.0, "annualized_sharpe": 0.0, "max_drawdown": 0.0, "avg_gross_exposure": 0.0, "bootstrap_positive_fraction_200": 0.0}
    equity = 1.0
    peak = 1.0
    dd = 0.0
    for v in values:
        equity *= max(0.0, 1.0 + v)
        peak = max(peak, equity)
        dd = max(dd, 0.0 if peak <= 0 else (peak - equity) / peak)
    sigma = pstdev(values)
    return {
        "days": len(values),
        "total_return": equity - 1.0,
        "annualized_sharpe": 0.0 if sigma <= 0 else sqrt(365.0) * mean(values) / sigma,
        "max_drawdown": dd,
        "avg_gross_exposure": mean(r.gross_exposure for r in rows),
        "avg_turnover": mean(r.turnover for r in rows),
        "avg_funding_return": mean(r.funding_return for r in rows),
        "bootstrap_positive_fraction_200": _block_bootstrap_positive_fraction(values),
    }


def _pass_gate(record):
    va = record["stress"]["validation"]
    oo = record["stress"]["oos"]
    tr = record["stress"]["train"]
    return bool(
        tr["days"] >= 300
        and va["days"] >= 300
        and oo["days"] >= 200
        and va["total_return"] > 0
        and va["annualized_sharpe"] >= 0.75
        and va["max_drawdown"] <= 0.30
        and va["avg_gross_exposure"] >= 0.75
        and oo["total_return"] > 0
        and oo["annualized_sharpe"] >= 0.50
        and oo["max_drawdown"] <= 0.30
        and oo["avg_gross_exposure"] >= 0.75
        and oo["bootstrap_positive_fraction_200"] >= 0.70
    )


def _load_symbol(symbol: str):
    candles, missing_price = fetch_price_history(symbol)
    funding, missing_funding = fetch_funding_history(symbol)
    return symbol, candles, funding, missing_price, missing_funding


def main() -> int:
    prices = {}
    funding = {}
    coverage = {}
    with ThreadPoolExecutor(max_workers=MAX_SYMBOL_WORKERS) as executor:
        futures = {executor.submit(_load_symbol, s): s for s in FIXED_UNIVERSE}
        for future in as_completed(futures):
            symbol, candles, points, missing_price, missing_funding = future.result()
            prices[symbol] = candles
            funding[symbol] = points
            coverage[symbol] = {
                "price_candles": len(candles),
                "funding_points": len(points),
                "missing_price_months": missing_price,
                "missing_funding_months": missing_funding,
            }
            print("DUAL_SIDE_COVERAGE", symbol, len(candles), len(points))

    daily = {s: candles_to_complete_utc_days(prices[s]) for s in FIXED_UNIVERSE}
    records = []
    for candidate in FROZEN_CANDIDATES:
        portfolio = _simulate(candidate, daily, funding)
        parts = split_calendar(portfolio)
        record = {
            "candidate": candidate.name,
            "family": candidate.family,
            "base": {k: _summary(v, "base_return") for k, v in parts.items()},
            "stress": {k: _summary(v, "stress_return") for k, v in parts.items()},
            "severe_25bps": {
                k: _summary(v, "stress_return", SEVERE_ROUND_TRIP_BPS - STRESS_ROUND_TRIP_BPS)
                for k, v in parts.items()
            },
        }
        record["historical_pass"] = _pass_gate(record)
        records.append(record)

    ranked = sorted(
        records,
        key=lambda r: (
            r["stress"]["validation"]["annualized_sharpe"],
            r["stress"]["validation"]["total_return"],
        ),
        reverse=True,
    )
    report = {
        "schema_version": "CRYPTO_DUAL_SIDED_STRATEGY_SWEEP_V1",
        "frozen_before_test": True,
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "universe_contract": "FIXED_20_PAIR_WITH_XLM_NO_FIL",
        "price_source": "BINANCE_PUBLIC_ARCHIVE_USDM_4H",
        "funding_source": "BINANCE_VISION_USDM_MONTHLY_FUNDING_RATE",
        "period": {"warmup": "2023", "train": "2024", "validation": "2025", "oos": "2026-01..2026-08"},
        "selection_partition": "validation",
        "oos_used_for_selection": False,
        "costs": {"base_round_trip_bps": 8.0, "stress_round_trip_bps": 14.0, "severe_round_trip_bps": 25.0},
        "direction_contract": "every strategy can hold both LONG and SHORT perpetual futures; no long-only fallback",
        "historical_gate": {"validation_sharpe_min": 0.75, "oos_sharpe_min": 0.50, "max_dd": 0.30, "oos_bootstrap_positive_min": 0.70, "avg_gross_exposure_min": 0.75},
        "historical_pass": [r["candidate"] for r in ranked if r["historical_pass"]],
        "validation_ranking": [r["candidate"] for r in ranked],
        "coverage": coverage,
        "rows": ranked,
    }
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
