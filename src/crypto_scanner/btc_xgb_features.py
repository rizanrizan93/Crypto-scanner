from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from statistics import mean, pstdev

from crypto_scanner.btc_xgb_data import FundingPoint, HourRow

HOUR_MS = 3_600_000
FEATURE_NAMES = (
    "return_1h", "return_3h", "return_6h", "return_12h", "return_24h", "return_72h",
    "realized_vol_24h", "realized_vol_72h", "atr_24h_over_close",
    "ema12_over_ema48_minus1", "rsi14_scaled", "volume_z24", "volume_z168",
    "taker_imbalance_1h", "taker_imbalance_6h", "funding_last", "funding_mean_24h",
)


@dataclass(frozen=True, slots=True)
class Sample:
    signal_time_ms: int
    entry_time_ms: int
    exit_time_ms: int
    entry_price: float
    exit_price: float
    features: tuple[float, ...]
    target_return: float


def _ema(values: list[float], span: int) -> list[float]:
    alpha = 2.0 / (span + 1.0)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1.0 - alpha) * output[-1])
    return output


def _true_ranges(rows: tuple[HourRow, ...]) -> list[float]:
    output: list[float] = []
    prior_close: float | None = None
    for row in rows:
        tr = row.high - row.low
        if prior_close is not None:
            tr = max(tr, abs(row.high - prior_close), abs(row.low - prior_close))
        output.append(tr)
        prior_close = row.close
    return output


def _imbalance(row: HourRow) -> float:
    if row.quote_volume <= 0:
        return 0.0
    return (2.0 * row.taker_buy_quote - row.quote_volume) / row.quote_volume


def _zscore(value: float, history: list[float]) -> float | None:
    if not history:
        return None
    sigma = pstdev(history)
    if sigma <= 1e-15:
        return None
    return (value - mean(history)) / sigma


def _funding_features(
    funding: tuple[FundingPoint, ...],
    funding_times: list[int],
    entry_ms: int,
) -> tuple[float, float]:
    end = bisect_left(funding_times, entry_ms)
    if end == 0:
        return 0.0, 0.0
    last = funding[end - 1].rate
    start = bisect_left(funding_times, entry_ms - 24 * HOUR_MS, 0, end)
    values = [funding[idx].rate for idx in range(start, end)]
    return last, mean(values) if values else 0.0


def build_samples(
    rows: tuple[HourRow, ...],
    funding: tuple[FundingPoint, ...],
) -> tuple[Sample, ...]:
    if len(rows) < 200:
        return ()
    closes = [row.close for row in rows]
    ema12 = _ema(closes, 12)
    ema48 = _ema(closes, 48)
    trs = _true_ranges(rows)
    hourly_returns = [0.0]
    for idx in range(1, len(rows)):
        prior = rows[idx - 1].close
        hourly_returns.append(0.0 if prior <= 0 else rows[idx].close / prior - 1.0)
    imbalances = [_imbalance(row) for row in rows]
    funding_times = [point.time_ms for point in funding]
    output: list[Sample] = []

    for idx in range(168, len(rows) - 5):
        if any(
            rows[j].start_time_ms - rows[j - 1].start_time_ms != HOUR_MS
            for j in range(idx - 167, idx + 6)
        ):
            continue
        current = rows[idx]
        entry = rows[idx + 1]
        exit_row = rows[idx + 5]
        if current.close <= 0 or entry.open <= 0:
            continue
        returns = []
        valid = True
        for lag in (1, 3, 6, 12, 24, 72):
            prior_close = rows[idx - lag].close
            if prior_close <= 0:
                valid = False
                break
            returns.append(current.close / prior_close - 1.0)
        if not valid:
            continue
        vol24 = pstdev(hourly_returns[idx - 23 : idx + 1])
        vol72 = pstdev(hourly_returns[idx - 71 : idx + 1])
        atr24 = mean(trs[idx - 23 : idx + 1]) / current.close
        ema_ratio = ema12[idx] / ema48[idx] - 1.0 if ema48[idx] > 0 else 0.0
        recent = hourly_returns[idx - 13 : idx + 1]
        gains = [max(value, 0.0) for value in recent]
        losses = [max(-value, 0.0) for value in recent]
        avg_gain = mean(gains)
        avg_loss = mean(losses)
        if avg_loss <= 1e-15:
            rsi = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - 100.0 / (1.0 + rs)
        rsi_scaled = (rsi - 50.0) / 50.0
        volume24 = [rows[j].quote_volume for j in range(idx - 24, idx)]
        volume168 = [rows[j].quote_volume for j in range(idx - 168, idx)]
        volume_z24 = _zscore(current.quote_volume, volume24)
        volume_z168 = _zscore(current.quote_volume, volume168)
        if volume_z24 is None or volume_z168 is None:
            continue
        funding_last, funding_mean24 = _funding_features(
            funding,
            funding_times,
            entry.start_time_ms,
        )
        features = tuple(
            returns
            + [
                vol24,
                vol72,
                atr24,
                ema_ratio,
                rsi_scaled,
                volume_z24,
                volume_z168,
                imbalances[idx],
                mean(imbalances[idx - 5 : idx + 1]),
                funding_last,
                funding_mean24,
            ]
        )
        if len(features) != len(FEATURE_NAMES):
            raise RuntimeError("BTC XGB feature vector shape mismatch")
        target = exit_row.open / entry.open - 1.0
        output.append(
            Sample(
                signal_time_ms=current.start_time_ms + HOUR_MS,
                entry_time_ms=entry.start_time_ms,
                exit_time_ms=exit_row.start_time_ms,
                entry_price=entry.open,
                exit_price=exit_row.open,
                features=features,
                target_return=target,
            )
        )
    return tuple(output)
