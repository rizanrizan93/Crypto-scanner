from __future__ import annotations

import csv
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import httpx

from crypto_scanner.binance_public_archive import verify_archive

BASE = "https://data.binance.vision/data/futures/um/monthly"
WORKERS = 8


@dataclass(frozen=True, slots=True)
class DailyBar:
    start_time_ms: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class FundingPoint:
    time_ms: int
    rate: float


def _normalize_ts(value: int) -> int:
    return value // 1000 if value > 100_000_000_000_000 else value


def _months():
    for year in range(2020, 2027):
        end_month = 8 if year == 2026 else 12
        for month in range(1, end_month + 1):
            yield year, month


def _get_verified(client: httpx.Client, url: str, filename: str) -> bytes | None:
    data = client.get(url)
    checksum = client.get(f"{url}.CHECKSUM")
    if data.status_code == 404 and checksum.status_code == 404:
        return None
    if data.status_code != 200 or checksum.status_code != 200:
        raise RuntimeError(
            f"ARCHIVE_FETCH_FAILED:{filename}:data={data.status_code}:checksum={checksum.status_code}"
        )
    verify_archive(data.content, checksum.text, filename)
    return data.content


def _parse_daily(payload: bytes) -> list[DailyBar]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) != 1:
            raise RuntimeError("daily archive must contain exactly one file")
        raw = archive.read(members[0]).decode("utf-8")
    output: list[DailyBar] = []
    for row in csv.reader(io.StringIO(raw)):
        if not row or not row[0].isdigit():
            continue
        if len(row) < 5:
            raise RuntimeError("unexpected daily kline schema")
        output.append(
            DailyBar(
                start_time_ms=_normalize_ts(int(row[0])),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
            )
        )
    return output


def _parse_funding(payload: bytes) -> list[FundingPoint]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) != 1:
            raise RuntimeError("funding archive must contain exactly one file")
        raw = archive.read(members[0]).decode("utf-8")
    reader = csv.DictReader(io.StringIO(raw))
    output: list[FundingPoint] = []
    for row in reader:
        if not row.get("calc_time"):
            continue
        output.append(
            FundingPoint(
                time_ms=_normalize_ts(int(row["calc_time"])),
                rate=float(row["last_funding_rate"]),
            )
        )
    return output


def _fetch_month(year: int, month: int):
    label = f"{year:04d}-{month:02d}"
    headers = {"User-Agent": "crypto-scanner-research/1.0"}
    with httpx.Client(timeout=45.0, headers=headers, follow_redirects=False) as client:
        price_name = f"BTCUSDT-1d-{label}.zip"
        price_url = f"{BASE}/klines/BTCUSDT/1d/{price_name}"
        price_payload = _get_verified(client, price_url, price_name)

        funding_name = f"BTCUSDT-fundingRate-{label}.zip"
        funding_url = f"{BASE}/fundingRate/BTCUSDT/{funding_name}"
        funding_payload = _get_verified(client, funding_url, funding_name)

    return (
        label,
        [] if price_payload is None else _parse_daily(price_payload),
        [] if funding_payload is None else _parse_funding(funding_payload),
        price_payload is None,
        funding_payload is None,
    )


def load_history() -> tuple[tuple[DailyBar, ...], tuple[FundingPoint, ...], list[str], list[str]]:
    bars: list[DailyBar] = []
    funding: list[FundingPoint] = []
    missing_price: list[str] = []
    missing_funding: list[str] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(_fetch_month, year, month) for year, month in _months()]
        for future in as_completed(futures):
            label, month_bars, month_funding, price_missing, funding_missing = future.result()
            bars.extend(month_bars)
            funding.extend(month_funding)
            if price_missing:
                missing_price.append(label)
            if funding_missing:
                missing_funding.append(label)

    bars.sort(key=lambda row: row.start_time_ms)
    funding.sort(key=lambda row: row.time_ms)
    missing_price.sort()
    missing_funding.sort()
    if any(b.start_time_ms <= a.start_time_ms for a, b in zip(bars, bars[1:], strict=False)):
        raise RuntimeError("daily history timestamps are not strictly increasing")
    if any(b.time_ms <= a.time_ms for a, b in zip(funding, funding[1:], strict=False)):
        raise RuntimeError("funding history timestamps are not strictly increasing")
    return tuple(bars), tuple(funding), missing_price, missing_funding
