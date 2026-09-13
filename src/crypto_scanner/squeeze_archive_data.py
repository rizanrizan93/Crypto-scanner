from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from crypto_scanner.binance_public_archive import BinancePublicArchiveClient, make_monthly_package, verify_archive


@dataclass(frozen=True, slots=True)
class FundingPoint:
    funding_time_ms: int
    funding_rate: float


def months():
    for year in range(2023, 2027):
        end_month = 8 if year == 2026 else 12
        for month in range(1, end_month + 1):
            yield year, month


def fetch_price_history(symbol: str):
    rows = []
    missing = []
    with BinancePublicArchiveClient(timeout_seconds=45.0) as client:
        for year, month in months():
            package = make_monthly_package(symbol, "4h", year, month)
            try:
                rows.extend(client.fetch_month(package))
            except Exception as exc:
                if "data=404 checksum=404" in str(exc):
                    missing.append(f"{year:04d}-{month:02d}")
                    continue
                raise
    rows.sort(key=lambda row: row.start_time_ms)
    return tuple(rows), missing


def _parse_funding(payload: bytes) -> list[FundingPoint]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        if len(members) != 1:
            raise RuntimeError("funding archive must contain one file")
        raw = archive.read(members[0]).decode("utf-8")
    reader = csv.DictReader(io.StringIO(raw))
    output = []
    for row in reader:
        if not row.get("calc_time"):
            continue
        output.append(FundingPoint(int(row["calc_time"]), float(row["last_funding_rate"])))
    return output


def fetch_funding_history(symbol: str):
    points: list[FundingPoint] = []
    missing = []
    headers = {"User-Agent": "crypto-scanner-research/1.0"}
    with httpx.Client(timeout=45.0, headers=headers, follow_redirects=False) as client:
        for year, month in months():
            filename = f"{symbol}-fundingRate-{year:04d}-{month:02d}.zip"
            url = (
                "https://data.binance.vision/data/futures/um/monthly/fundingRate/"
                f"{symbol}/{filename}"
            )
            data = client.get(url)
            checksum = client.get(f"{url}.CHECKSUM")
            if data.status_code == 404 and checksum.status_code == 404:
                missing.append(f"{year:04d}-{month:02d}")
                continue
            if data.status_code != 200 or checksum.status_code != 200:
                raise RuntimeError(
                    f"FUNDING_FETCH_FAILED:{symbol}:{year:04d}-{month:02d}:"
                    f"data={data.status_code}:checksum={checksum.status_code}"
                )
            verify_archive(data.content, checksum.text, filename)
            points.extend(_parse_funding(data.content))
    points.sort(key=lambda row: row.funding_time_ms)
    if any(b.funding_time_ms <= a.funding_time_ms for a, b in zip(points, points[1:], strict=False)):
        raise RuntimeError(f"duplicate funding timestamps for {symbol}")
    return tuple(points), missing
