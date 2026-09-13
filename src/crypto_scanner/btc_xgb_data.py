from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass

import httpx

from crypto_scanner.binance_public_archive import verify_archive

BASE = "https://data.binance.vision/data/futures/um/monthly"


@dataclass(frozen=True, slots=True)
class HourRow:
    start_time_ms: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float
    taker_buy_quote: float


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


def _parse_kline(payload: bytes) -> list[HourRow]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) != 1:
            raise RuntimeError("BTC 1h archive must contain exactly one file")
        raw = archive.read(members[0]).decode("utf-8")
    output: list[HourRow] = []
    for row in csv.reader(io.StringIO(raw)):
        if not row or not row[0].isdigit():
            continue
        if len(row) < 11:
            raise RuntimeError("unexpected BTC 1h kline schema")
        output.append(
            HourRow(
                start_time_ms=_normalize_ts(int(row[0])),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                quote_volume=float(row[7]),
                taker_buy_quote=float(row[10]),
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


def load_btc_history() -> tuple[tuple[HourRow, ...], tuple[FundingPoint, ...], list[str], list[str]]:
    hours: list[HourRow] = []
    funding: list[FundingPoint] = []
    missing_price: list[str] = []
    missing_funding: list[str] = []
    headers = {"User-Agent": "crypto-scanner-research/1.0"}
    with httpx.Client(timeout=45.0, headers=headers, follow_redirects=False) as client:
        for year, month in _months():
            label = f"{year:04d}-{month:02d}"
            kline_name = f"BTCUSDT-1h-{label}.zip"
            kline_url = f"{BASE}/klines/BTCUSDT/1h/{kline_name}"
            kline_payload = _get_verified(client, kline_url, kline_name)
            if kline_payload is None:
                missing_price.append(label)
            else:
                hours.extend(_parse_kline(kline_payload))

            funding_name = f"BTCUSDT-fundingRate-{label}.zip"
            funding_url = f"{BASE}/fundingRate/BTCUSDT/{funding_name}"
            funding_payload = _get_verified(client, funding_url, funding_name)
            if funding_payload is None:
                missing_funding.append(label)
            else:
                funding.extend(_parse_funding(funding_payload))

    hours.sort(key=lambda row: row.start_time_ms)
    funding.sort(key=lambda row: row.time_ms)
    if any(b.start_time_ms <= a.start_time_ms for a, b in zip(hours, hours[1:], strict=False)):
        raise RuntimeError("BTC hourly history has duplicate/non-increasing timestamps")
    if any(b.time_ms <= a.time_ms for a, b in zip(funding, funding[1:], strict=False)):
        raise RuntimeError("BTC funding history has duplicate/non-increasing timestamps")
    return tuple(hours), tuple(funding), missing_price, missing_funding
