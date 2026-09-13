from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass

import httpx

from crypto_scanner.binance_public_archive import verify_archive


@dataclass(frozen=True, slots=True)
class DailyBar:
    start_time_ms: int
    open: float
    high: float
    low: float
    close: float


def months():
    for year in range(2023, 2027):
        end_month = 8 if year == 2026 else 12
        for month in range(1, end_month + 1):
            yield year, month


def _parse(payload: bytes) -> list[DailyBar]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        if len(members) != 1:
            raise RuntimeError("D1 archive must contain one file")
        raw = archive.read(members[0]).decode("utf-8")
    output = []
    for row in csv.reader(io.StringIO(raw)):
        if row and row[0].isdigit():
            output.append(DailyBar(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4])))
    return output


def fetch_btc_d1() -> tuple[DailyBar, ...]:
    rows: list[DailyBar] = []
    with httpx.Client(timeout=45.0, follow_redirects=False) as client:
        for year, month in months():
            label = f"{year:04d}-{month:02d}"
            filename = f"BTCUSDT-1d-{label}.zip"
            url = f"https://data.binance.vision/data/futures/um/monthly/klines/BTCUSDT/1d/{filename}"
            data = client.get(url)
            checksum = client.get(f"{url}.CHECKSUM")
            if data.status_code != 200 or checksum.status_code != 200:
                raise RuntimeError(f"BTC_D1_FETCH_FAILED:{label}:{data.status_code}:{checksum.status_code}")
            verify_archive(data.content, checksum.text, filename)
            rows.extend(_parse(data.content))
    rows.sort(key=lambda x: x.start_time_ms)
    if any(b.start_time_ms <= a.start_time_ms for a, b in zip(rows, rows[1:], strict=False)):
        raise RuntimeError("BTC_D1_NON_INCREASING")
    return tuple(rows)
