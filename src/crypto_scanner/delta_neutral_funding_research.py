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

CORE5 = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")
START = date(2023, 1, 1)
END_EXCLUSIVE = date(2026, 9, 1)
BASE_RT_BPS = 15.0
STRESS_RT_BPS = 25.0
SEVERE_RT_BPS = 40.0


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    universe: tuple[str, ...]
    lookback_days: int
    top_k: int
    rebalance_days: int


FROZEN_CANDIDATES = (
    Candidate("DNC_BTC_7D_WEEKLY", ("BTCUSDT",), 7, 1, 7),
    Candidate("DNC_BTCETH_7D_WEEKLY", ("BTCUSDT", "ETHUSDT"), 7, 2, 7),
    Candidate("DNC_CORE5_7D_TOP3_WEEKLY", CORE5, 7, 3, 7),
    Candidate("DNC_CORE5_14D_TOP3_WEEKLY", CORE5, 14, 3, 7),
    Candidate("DNC_CORE5_28D_TOP3_WEEKLY", CORE5, 28, 3, 7),
)


@dataclass(frozen=True, slots=True)
class FundingPoint:
    time_ms: int
    rate: float


@dataclass(frozen=True, slots=True)
class DailyRow:
    day: date
    base_return: float
    stress_return: float
    severe_return: float
    gross_exposure: float
    basis_return: float
    base_funding_return: float
    stress_funding_return: float
    turnover: float


def _norm_ms(value: int) -> int:
    # Binance spot archives moved to microsecond timestamps in 2025.
    return value // 1000 if value > 100_000_000_000_000 else value


def _checksum(text: str, filename: str) -> str:
    parts = text.strip().split()
    if len(parts) < 2 or parts[-1].lstrip("*") != filename:
        raise RuntimeError("checksum manifest filename mismatch")
    digest = parts[0].lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise RuntimeError("invalid SHA-256 manifest")
    return digest


def _verify(payload: bytes, checksum_text: str, filename: str) -> None:
    if hashlib.sha256(payload).hexdigest() != _checksum(checksum_text, filename):
        raise RuntimeError("archive SHA-256 mismatch")


def _single_csv(payload: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [x for x in archive.infolist() if not x.is_dir()]
            if len(members) != 1 or members[0].file_size > 64 * 1024 * 1024:
                raise RuntimeError("unexpected archive shape")
            return archive.read(members[0]).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise RuntimeError("invalid archive") from exc


def _parse_daily_open(payload: bytes) -> dict[date, float]:
    result: dict[date, float] = {}
    reader = csv.reader(io.StringIO(_single_csv(payload)))
    for row in reader:
        if not row or not row[0].isdigit():
            continue
        if len(row) < 5:
            raise RuntimeError("kline schema mismatch")
        stamp = datetime.fromtimestamp(_norm_ms(int(row[0])) / 1000, tz=UTC)
        if stamp.hour != 0 or stamp.minute != 0:
            raise RuntimeError("daily kline is not UTC anchored")
        px = float(row[1])
        if not math.isfinite(px) or px <= 0:
            raise RuntimeError("invalid kline open")
        if START <= stamp.date() < END_EXCLUSIVE:
            if stamp.date() in result:
                raise RuntimeError("duplicate daily kline")
            result[stamp.date()] = px
    return result


def _parse_funding(payload: bytes) -> tuple[FundingPoint, ...]:
    reader = csv.DictReader(io.StringIO(_single_csv(payload)))
    required = {"calc_time", "funding_interval_hours", "last_funding_rate"}
    if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
        raise RuntimeError("funding schema mismatch")
    rows: list[FundingPoint] = []
    for row in reader:
        raw_time = _norm_ms(int(row["calc_time"]))
        interval = int(row["funding_interval_hours"])
        rate = float(row["last_funding_rate"])
        if interval <= 0 or not math.isfinite(rate):
            raise RuntimeError("invalid funding row")
        stamp = datetime.fromtimestamp(raw_time / 1000, tz=UTC).date()
        if START <= stamp < END_EXCLUSIVE:
            rows.append(FundingPoint(raw_time, rate))
    rows.sort(key=lambda x: x.time_ms)
    if any(b.time_ms <= a.time_ms for a, b in zip(rows, rows[1:], strict=False)):
        raise RuntimeError("duplicate/non-increasing funding timestamps")
    return tuple(rows)


def _months():
    year, month = 2023, 1
    while (year, month) <= (2026, 8):
        yield year, month
        month += 1
        if month == 13:
            year += 1
            month = 1


def _fetch_archive(client: httpx.Client, url: str) -> bytes | None:
    data = client.get(url)
    checksum = client.get(f"{url}.CHECKSUM")
    if data.status_code == 404 and checksum.status_code == 404:
        return None
    if data.status_code != 200 or checksum.status_code != 200:
        raise RuntimeError(
            f"archive fetch failed data={data.status_code} checksum={checksum.status_code} url={url}"
        )
    filename = url.rsplit("/", 1)[-1]
    _verify(data.content, checksum.text, filename)
    return data.content


def load_symbol(symbol: str) -> tuple[dict[date, float], dict[date, float], tuple[FundingPoint, ...], dict[str, list[str]]]:
    spot: dict[date, float] = {}
    perp: dict[date, float] = {}
    funding: list[FundingPoint] = []
    missing = {"spot": [], "perp": [], "funding": []}
    with httpx.Client(timeout=30.0, follow_redirects=False, headers={"User-Agent": "crypto-scanner-research/1.0"}) as client:
        for year, month in _months():
            label = f"{year:04d}-{month:02d}"
            paths = {
                "spot": f"https://data.binance.vision/data/spot/monthly/klines/{symbol}/1d/{symbol}-1d-{label}.zip",
                "perp": f"https://data.binance.vision/data/futures/um/monthly/klines/{symbol}/1d/{symbol}-1d-{label}.zip",
                "funding": f"https://data.binance.vision/data/futures/um/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{label}.zip",
            }
            for kind, url in paths.items():
                payload = _fetch_archive(client, url)
                if payload is None:
                    missing[kind].append(label)
                    continue
                if kind == "spot":
                    overlap = set(spot) & set(_parse_daily_open(payload))
                    if overlap:
                        raise RuntimeError(f"duplicate spot dates {symbol}")
                    spot.update(_parse_daily_open(payload))
                elif kind == "perp":
                    parsed = _parse_daily_open(payload)
                    if set(perp) & set(parsed):
                        raise RuntimeError(f"duplicate perp dates {symbol}")
                    perp.update(parsed)
                else:
                    funding.extend(_parse_funding(payload))
    funding.sort(key=lambda x: x.time_ms)
    if any(b.time_ms <= a.time_ms for a, b in zip(funding, funding[1:], strict=False)):
        raise RuntimeError(f"duplicate funding history {symbol}")
    return spot, perp, tuple(funding), missing


def _funding_sum(points: tuple[FundingPoint, ...], start_ms: int, end_ms: int) -> float:
    return sum(point.rate for point in points if start_ms <= point.time_ms < end_ms)


def _funding_between(points: tuple[FundingPoint, ...], entry_ms: int, exit_ms: int) -> float:
    return sum(point.rate for point in points if entry_ms < point.time_ms < exit_ms)


def _stress_funding(value: float) -> float:
    return value * (0.8 if value >= 0 else 1.2)


def simulate(data: dict[str, tuple[dict[date, float], dict[date, float], tuple[FundingPoint, ...]]], candidate: Candidate) -> tuple[DailyRow, ...]:
    all_days = [START + timedelta(days=i) for i in range((END_EXCLUSIVE - START).days)]
    previous_spot = {s: 0.0 for s in candidate.universe}
    previous_perp = {s: 0.0 for s in candidate.universe}
    current_spot = dict(previous_spot)
    current_perp = dict(previous_perp)
    rows: list[DailyRow] = []

    for i, day in enumerate(all_days[:-1]):
        next_day = day + timedelta(days=1)
        entry_ms = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
        exit_ms = int(datetime(next_day.year, next_day.month, next_day.day, tzinfo=UTC).timestamp() * 1000)

        if i % candidate.rebalance_days == 0:
            ranked: list[tuple[float, str]] = []
            lookback_ms = int((datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(days=candidate.lookback_days)).timestamp() * 1000)
            for symbol in candidate.universe:
                spot, perp, funding = data[symbol]
                if day not in spot or next_day not in spot or day not in perp or next_day not in perp:
                    continue
                score = _funding_sum(funding, lookback_ms, entry_ms)
                if score > 0:
                    ranked.append((score, symbol))
            ranked.sort(reverse=True)
            selected = [symbol for _, symbol in ranked[: candidate.top_k]]
            current_spot = {s: 0.0 for s in candidate.universe}
            current_perp = {s: 0.0 for s in candidate.universe}
            if selected:
                leg = 0.5 / len(selected)
                for symbol in selected:
                    current_spot[symbol] = leg
                    current_perp[symbol] = -leg

        valid = True
        for symbol in candidate.universe:
            if current_spot[symbol] == 0 and current_perp[symbol] == 0:
                continue
            spot, perp, _ = data[symbol]
            if day not in spot or next_day not in spot or day not in perp or next_day not in perp:
                valid = False
                break
        if not valid:
            current_spot = {s: 0.0 for s in candidate.universe}
            current_perp = {s: 0.0 for s in candidate.universe}

        turnover = sum(abs(current_spot[s] - previous_spot[s]) + abs(current_perp[s] - previous_perp[s]) for s in candidate.universe)
        basis_ret = 0.0
        funding_base = 0.0
        funding_stress = 0.0
        for symbol in candidate.universe:
            sw = current_spot[symbol]
            pw = current_perp[symbol]
            if sw == 0 and pw == 0:
                continue
            spot, perp, funding = data[symbol]
            sr = spot[next_day] / spot[day] - 1.0
            pr = perp[next_day] / perp[day] - 1.0
            basis_ret += sw * sr + pw * pr
            realized = _funding_between(funding, entry_ms, exit_ms)
            # A short perp receives positive funding; pw is negative.
            funding_component = -pw * realized
            funding_base += funding_component
            funding_stress += _stress_funding(funding_component)

        base_cost = turnover * (BASE_RT_BPS / 2) / 10000
        stress_cost = turnover * (STRESS_RT_BPS / 2) / 10000
        severe_cost = turnover * (SEVERE_RT_BPS / 2) / 10000
        gross = sum(abs(current_spot[s]) + abs(current_perp[s]) for s in candidate.universe)
        rows.append(DailyRow(day, basis_ret + funding_base - base_cost, basis_ret + funding_stress - stress_cost, basis_ret + funding_stress - severe_cost, gross, basis_ret, funding_base, funding_stress, turnover))
        previous_spot = dict(current_spot)
        previous_perp = dict(current_perp)
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
    positives = 0
    for _ in range(trials):
        sample: list[float] = []
        while len(sample) < len(values):
            start = rng.randrange(len(values))
            sample.extend(values[(start + j) % len(values)] for j in range(block))
        if _compound(sample[: len(values)]) > 0:
            positives += 1
    return positives / trials


def summarize(rows: tuple[DailyRow, ...], field: str) -> dict[str, float | int]:
    values = [float(getattr(row, field)) for row in rows]
    mean = fmean(values) if values else 0.0
    vol = pstdev(values) if len(values) > 1 else 0.0
    return {
        "days": len(rows),
        "total_return": _compound(values),
        "annualized_sharpe": (mean / vol * math.sqrt(365)) if vol > 0 else 0.0,
        "max_drawdown": _max_dd(values),
        "win_rate": sum(v > 0 for v in values) / len(values) if values else 0.0,
        "avg_gross_exposure": fmean(row.gross_exposure for row in rows) if rows else 0.0,
        "avg_turnover": fmean(row.turnover for row in rows) if rows else 0.0,
        "avg_basis_return": fmean(row.basis_return for row in rows) if rows else 0.0,
        "avg_funding_return": fmean(row.base_funding_return for row in rows) if rows else 0.0,
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
    return bool(
        tr["days"] >= 300 and va["days"] >= 300 and oo["days"] >= 200
        and tr["total_return"] > 0
        and va["total_return"] > 0 and va["annualized_sharpe"] >= 0.75 and va["max_drawdown"] <= 0.20 and va["avg_gross_exposure"] >= 0.20
        and oo["total_return"] > 0 and oo["annualized_sharpe"] >= 0.50 and oo["max_drawdown"] <= 0.20 and oo["avg_gross_exposure"] >= 0.20
        and oo["bootstrap_positive_fraction_200"] >= 0.70
    )


def build_report() -> dict[str, object]:
    raw = {}
    coverage = {}
    for symbol in CORE5:
        spot, perp, funding, missing = load_symbol(symbol)
        raw[symbol] = (spot, perp, funding)
        coverage[symbol] = {"spot_days": len(spot), "perp_days": len(perp), "funding_points": len(funding), "missing": missing}
        print(f"DNC_COVERAGE {symbol} spot={len(spot)} perp={len(perp)} funding={len(funding)}")

    records = []
    for candidate in FROZEN_CANDIDATES:
        parts = partition(simulate(raw, candidate))
        record = {
            "candidate": candidate.name,
            "parameters": {"universe": list(candidate.universe), "lookback_days": candidate.lookback_days, "top_k": candidate.top_k, "rebalance_days": candidate.rebalance_days},
            "base": {key: summarize(value, "base_return") for key, value in parts.items()},
            "stress": {key: summarize(value, "stress_return") for key, value in parts.items()},
            "severe": {key: summarize(value, "severe_return") for key, value in parts.items()},
        }
        record["historical_pass"] = passes(record)
        records.append(record)
    ranked = sorted(records, key=lambda row: (row["stress"]["validation"]["annualized_sharpe"], row["stress"]["validation"]["total_return"], -row["stress"]["validation"]["max_drawdown"]), reverse=True)
    return {
        "schema_version": "CRYPTO_DELTA_NEUTRAL_FUNDING_CARRY_V1",
        "research_only": True,
        "execution_influence": False,
        "live_execution_enabled": False,
        "sources": ["BINANCE_VISION_SPOT_1D", "BINANCE_VISION_USDM_PERP_1D", "BINANCE_VISION_USDM_FUNDING"],
        "integrity": "monthly ZIP sibling .CHECKSUM SHA-256 required; fail closed",
        "selection_partition": "validation",
        "oos_used_for_selection": False,
        "period": {"warmup": "2023", "train": "2024", "validation": "2025", "oos": "2026-01..2026-08"},
        "costs": {"base_round_trip_bps_on_gross": BASE_RT_BPS, "stress_round_trip_bps_on_gross": STRESS_RT_BPS, "severe_round_trip_bps_on_gross": SEVERE_RT_BPS, "stress_positive_funding_haircut": 0.20},
        "economic_rule": "long spot + short same-symbol USD-M perpetual; only positive trailing funding; no directional long/short crypto beta target",
        "historical_gate": {"train_stress_return": ">0", "validation_stress_return": ">0", "validation_sharpe_min": 0.75, "oos_stress_return": ">0", "oos_sharpe_min": 0.50, "max_dd": 0.20, "min_exposure": 0.20, "oos_bootstrap_positive_min": 0.70},
        "coverage": coverage,
        "validation_ranking": [row["candidate"] for row in ranked],
        "historical_pass": [row["candidate"] for row in ranked if row["historical_pass"]],
        "rows": ranked,
    }


def main() -> int:
    report = build_report()
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
