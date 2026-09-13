from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import random
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from statistics import fmean, pstdev

import httpx

UNIVERSE = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")
START = date(2023, 1, 1)
END_EXCLUSIVE = date(2026, 9, 1)
BASE_RT_BPS = 8.0
STRESS_RT_BPS = 14.0


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    horizons: tuple[int, int, int]


FROZEN_CANDIDATES = (
    Candidate("STE_DONCHIAN_20_60_120", (20, 60, 120)),
    Candidate("STE_DONCHIAN_20_55_100", (20, 55, 100)),
    Candidate("STE_DONCHIAN_30_90_180", (30, 90, 180)),
)


@dataclass(frozen=True, slots=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class FundingPoint:
    time_ms: int
    rate: float


@dataclass(frozen=True, slots=True)
class DailyRow:
    day: date
    base_return: float
    stress_return: float
    gross_exposure: float
    turnover: float
    price_return: float
    funding_return: float


def _norm_ms(value: int) -> int:
    return value // 1000 if value > 100_000_000_000_000 else value


def _manifest_digest(text: str, filename: str) -> str:
    parts = text.strip().split()
    if len(parts) < 2 or parts[-1].lstrip("*") != filename:
        raise RuntimeError("checksum filename mismatch")
    digest = parts[0].lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise RuntimeError("invalid SHA-256 manifest")
    return digest


def _verify(payload: bytes, manifest: str, filename: str) -> None:
    if hashlib.sha256(payload).hexdigest() != _manifest_digest(manifest, filename):
        raise RuntimeError("archive SHA-256 mismatch")


def _single_csv(payload: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1 or members[0].file_size > 64 * 1024 * 1024:
                raise RuntimeError("unexpected archive shape")
            return archive.read(members[0]).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise RuntimeError("invalid archive") from exc


def _parse_bars(payload: bytes) -> dict[date, Bar]:
    rows: dict[date, Bar] = {}
    for row in csv.reader(io.StringIO(_single_csv(payload))):
        if not row or not row[0].isdigit():
            continue
        stamp = datetime.fromtimestamp(_norm_ms(int(row[0])) / 1000, tz=UTC)
        if stamp.hour != 0 or stamp.minute != 0:
            raise RuntimeError("D1 archive is not UTC anchored")
        values = tuple(float(row[index]) for index in (1, 2, 3, 4))
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise RuntimeError("invalid OHLC")
        if START <= stamp.date() < END_EXCLUSIVE:
            if stamp.date() in rows:
                raise RuntimeError("duplicate D1 bar")
            rows[stamp.date()] = Bar(stamp.date(), *values)
    return rows


def _parse_funding(payload: bytes) -> tuple[FundingPoint, ...]:
    reader = csv.DictReader(io.StringIO(_single_csv(payload)))
    required = {"calc_time", "funding_interval_hours", "last_funding_rate"}
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise RuntimeError("funding schema mismatch")
    rows = []
    for row in reader:
        stamp = _norm_ms(int(row["calc_time"]))
        interval = int(row["funding_interval_hours"])
        rate = float(row["last_funding_rate"])
        if interval <= 0 or not math.isfinite(rate):
            raise RuntimeError("invalid funding row")
        day = datetime.fromtimestamp(stamp / 1000, tz=UTC).date()
        if START <= day < END_EXCLUSIVE:
            rows.append(FundingPoint(stamp, rate))
    rows.sort(key=lambda point: point.time_ms)
    if any(b.time_ms <= a.time_ms for a, b in zip(rows, rows[1:], strict=False)):
        raise RuntimeError("non-increasing funding history")
    return tuple(rows)


def _months():
    year, month = 2023, 1
    while (year, month) <= (2026, 8):
        yield year, month
        month += 1
        if month == 13:
            year += 1
            month = 1


def _fetch(client: httpx.Client, url: str) -> bytes | None:
    data = client.get(url)
    checksum = client.get(f"{url}.CHECKSUM")
    if data.status_code == 404 and checksum.status_code == 404:
        return None
    if data.status_code != 200 or checksum.status_code != 200:
        raise RuntimeError(f"archive fetch failed data={data.status_code} checksum={checksum.status_code}")
    filename = url.rsplit("/", 1)[-1]
    _verify(data.content, checksum.text, filename)
    return data.content


def load_symbol(symbol: str) -> tuple[dict[date, Bar], tuple[FundingPoint, ...]]:
    bars: dict[date, Bar] = {}
    funding: list[FundingPoint] = []
    with httpx.Client(timeout=30.0, follow_redirects=False, headers={"User-Agent": "crypto-scanner-research/1.0"}) as client:
        for year, month in _months():
            label = f"{year:04d}-{month:02d}"
            bar_url = f"https://data.binance.vision/data/futures/um/monthly/klines/{symbol}/1d/{symbol}-1d-{label}.zip"
            funding_url = f"https://data.binance.vision/data/futures/um/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{label}.zip"
            payload = _fetch(client, bar_url)
            if payload is not None:
                parsed = _parse_bars(payload)
                if set(parsed) & set(bars):
                    raise RuntimeError(f"duplicate bars for {symbol}")
                bars.update(parsed)
            funding_payload = _fetch(client, funding_url)
            if funding_payload is not None:
                funding.extend(_parse_funding(funding_payload))
    funding.sort(key=lambda point: point.time_ms)
    if any(b.time_ms <= a.time_ms for a, b in zip(funding, funding[1:], strict=False)):
        raise RuntimeError(f"duplicate funding points for {symbol}")
    return bars, tuple(funding)


def _funding_between(points: tuple[FundingPoint, ...], entry_ms: int, exit_ms: int) -> float:
    return sum(point.rate for point in points if entry_ms < point.time_ms < exit_ms)


def _stress_funding(value: float) -> float:
    return value * (0.8 if value >= 0 else 1.2)


def _signal_series(bars: dict[date, Bar], horizons: tuple[int, int, int]) -> dict[date, float]:
    ordered = [bars[day] for day in sorted(bars)]
    states = {horizon: 0 for horizon in horizons}
    signals: dict[date, float] = {}
    for index, bar in enumerate(ordered):
        for horizon in horizons:
            if index < horizon:
                continue
            history = ordered[index - horizon:index]
            if bar.close > max(item.high for item in history):
                states[horizon] = 1
            elif bar.close < min(item.low for item in history):
                states[horizon] = -1
        signals[bar.day] = sum(states.values()) / len(horizons)
    return signals


def _realized_vol(bars: dict[date, Bar], signal_day: date, window: int = 20) -> float | None:
    ordered_days = [day for day in sorted(bars) if day <= signal_day]
    if len(ordered_days) < window + 1:
        return None
    closes = [bars[day].close for day in ordered_days[-(window + 1):]]
    returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
    vol = pstdev(returns) * math.sqrt(365)
    return vol if vol > 1e-6 else None


def simulate(data: dict[str, tuple[dict[date, Bar], tuple[FundingPoint, ...]]], candidate: Candidate) -> tuple[DailyRow, ...]:
    signals = {symbol: _signal_series(data[symbol][0], candidate.horizons) for symbol in UNIVERSE}
    calendar = sorted(set.intersection(*(set(data[symbol][0]) for symbol in UNIVERSE)))
    previous_weights = {symbol: 0.0 for symbol in UNIVERSE}
    rows = []
    for index in range(1, len(calendar) - 1):
        signal_day = calendar[index - 1]
        entry_day = calendar[index]
        exit_day = calendar[index + 1]
        raw = {}
        for symbol in UNIVERSE:
            signal = signals[symbol].get(signal_day, 0.0)
            vol = _realized_vol(data[symbol][0], signal_day)
            raw[symbol] = 0.0 if vol is None else signal / vol
        gross_raw = sum(abs(value) for value in raw.values())
        weights = {symbol: (raw[symbol] / gross_raw if gross_raw > 0 else 0.0) for symbol in UNIVERSE}
        turnover = sum(abs(weights[symbol] - previous_weights[symbol]) for symbol in UNIVERSE)
        price_return = 0.0
        funding_base = 0.0
        funding_stress = 0.0
        entry_ms = int(datetime(entry_day.year, entry_day.month, entry_day.day, tzinfo=UTC).timestamp() * 1000)
        exit_ms = int(datetime(exit_day.year, exit_day.month, exit_day.day, tzinfo=UTC).timestamp() * 1000)
        for symbol, weight in weights.items():
            if weight == 0:
                continue
            bars, funding = data[symbol]
            price_return += weight * (bars[exit_day].open / bars[entry_day].open - 1.0)
            paid = -weight * _funding_between(funding, entry_ms, exit_ms)
            funding_base += paid
            funding_stress += _stress_funding(paid)
        base_cost = turnover * (BASE_RT_BPS / 2) / 10000
        stress_cost = turnover * (STRESS_RT_BPS / 2) / 10000
        rows.append(DailyRow(entry_day, price_return + funding_base - base_cost, price_return + funding_stress - stress_cost, sum(abs(v) for v in weights.values()), turnover, price_return, funding_base))
        previous_weights = weights
    return tuple(rows)


def _compound(values: list[float]) -> float:
    equity = 1.0
    for value in values:
        equity *= 1.0 + value
    return equity - 1.0


def _max_dd(values: list[float]) -> float:
    equity = peak = 1.0
    worst = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return worst


def _bootstrap_positive(values: list[float], trials: int = 200, block: int = 14) -> float:
    if not values:
        return 0.0
    rng = random.Random(20260913)
    positive = 0
    for _ in range(trials):
        sample = []
        while len(sample) < len(values):
            start = rng.randrange(len(values))
            sample.extend(values[(start + step) % len(values)] for step in range(block))
        positive += _compound(sample[:len(values)]) > 0
    return positive / trials


def summarize(rows: tuple[DailyRow, ...], field: str) -> dict[str, float | int]:
    values = [float(getattr(row, field)) for row in rows]
    mean = fmean(values) if values else 0.0
    vol = pstdev(values) if len(values) > 1 else 0.0
    return {
        "days": len(rows),
        "total_return": _compound(values),
        "annualized_sharpe": mean / vol * math.sqrt(365) if vol > 0 else 0.0,
        "max_drawdown": _max_dd(values),
        "win_rate": sum(value > 0 for value in values) / len(values) if values else 0.0,
        "avg_gross_exposure": fmean(row.gross_exposure for row in rows) if rows else 0.0,
        "avg_turnover": fmean(row.turnover for row in rows) if rows else 0.0,
        "bootstrap_positive_fraction_200": _bootstrap_positive(values),
    }


def partition(rows: tuple[DailyRow, ...]) -> dict[str, tuple[DailyRow, ...]]:
    return {
        "train": tuple(row for row in rows if date(2024, 1, 1) <= row.day <= date(2024, 12, 31)),
        "validation": tuple(row for row in rows if date(2025, 1, 1) <= row.day <= date(2025, 12, 31)),
        "oos": tuple(row for row in rows if date(2026, 1, 1) <= row.day <= date(2026, 8, 31)),
    }


def passes(record: dict[str, object]) -> bool:
    stress = record["stress"]
    assert isinstance(stress, dict)
    tr, va, oo = stress["train"], stress["validation"], stress["oos"]
    assert isinstance(tr, dict) and isinstance(va, dict) and isinstance(oo, dict)
    return bool(tr["days"] >= 300 and va["days"] >= 300 and oo["days"] >= 200 and tr["total_return"] > 0 and va["total_return"] > 0 and va["annualized_sharpe"] >= 0.75 and va["max_drawdown"] <= 0.30 and va["avg_gross_exposure"] >= 0.50 and oo["total_return"] > 0 and oo["annualized_sharpe"] >= 0.50 and oo["max_drawdown"] <= 0.30 and oo["avg_gross_exposure"] >= 0.50 and oo["bootstrap_positive_fraction_200"] >= 0.70)


def main() -> int:
    data = {}
    for symbol in UNIVERSE:
        bars, funding = load_symbol(symbol)
        data[symbol] = (bars, funding)
        print(f"STE_COVERAGE {symbol} bars={len(bars)} funding={len(funding)}")
    records = []
    for candidate in FROZEN_CANDIDATES:
        parts = partition(simulate(data, candidate))
        record = {
            "candidate": candidate.name,
            "parameters": {"horizons": list(candidate.horizons)},
            "base": {key: summarize(value, "base_return") for key, value in parts.items()},
            "stress": {key: summarize(value, "stress_return") for key, value in parts.items()},
        }
        record["historical_pass"] = passes(record)
        records.append(record)
    ranked = sorted(records, key=lambda row: (row["stress"]["validation"]["annualized_sharpe"], row["stress"]["validation"]["total_return"]), reverse=True)
    report = {
        "schema_version": "CRYPTO_SLOW_TREND_ENSEMBLE_V1",
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "universe": list(UNIVERSE),
        "source": "BINANCE_VISION_USDM_1D_PLUS_FUNDING",
        "integrity": "monthly ZIP sibling .CHECKSUM SHA-256 required; fail closed",
        "selection_partition": "validation",
        "oos_used_for_selection": False,
        "period": {"warmup": "2023", "train": "2024", "validation": "2025", "oos": "2026-01..2026-08"},
        "costs": {"base_round_trip_bps": BASE_RT_BPS, "stress_round_trip_bps": STRESS_RT_BPS, "stress_funding_benefit_haircut": 0.20},
        "execution": "signal on completed D1 close; next UTC D1 open; daily open-to-open mark",
        "historical_pass": [row["candidate"] for row in ranked if row["historical_pass"]],
        "validation_ranking": [row["candidate"] for row in ranked],
        "rows": ranked,
    }
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
