from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, date, datetime

import httpx

from crypto_scanner.binance_public_archive import BinancePublicArchiveClient, make_monthly_package, verify_archive
from crypto_scanner.funding_carry_research import FundingPoint
from crypto_scanner.run_funding_carry_research import fetch_funding_history

START = datetime(2023, 12, 1, tzinfo=UTC)
END = datetime(2026, 9, 1, tzinfo=UTC)


def months():
    cursor = date(2023, 12, 1)
    end = date(2026, 8, 1)
    while cursor <= end:
        yield cursor.year, cursor.month
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)


def _premium_url(symbol: str, year: int, month: int) -> tuple[str, str]:
    filename = f"{symbol}-5m-{year:04d}-{month:02d}.zip"
    url = (
        "https://data.binance.vision/data/futures/um/monthly/"
        f"premiumIndexKlines/{symbol}/5m/{filename}"
    )
    return url, filename


def _parse_premium(payload: bytes) -> list[tuple[int, float]]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            if len(members) != 1:
                raise RuntimeError("premium archive must contain one file")
            raw = archive.read(members[0]).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise RuntimeError("invalid premium archive") from exc
    output: list[tuple[int, float]] = []
    for row in csv.reader(io.StringIO(raw)):
        if not row or not row[0].isdigit():
            continue
        if len(row) < 5:
            raise RuntimeError("unexpected premium kline schema")
        start_ms = int(row[0])
        stamp = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
        if START <= stamp < END and stamp.minute == 55:
            output.append((start_ms + 300_000, float(row[4])))
    return output


def load_symbol(symbol: str) -> tuple[str, tuple[tuple[int, float], ...], dict[int, float], tuple[FundingPoint, ...]]:
    premium: list[tuple[int, float]] = []
    hourly_opens: dict[int, float] = {}
    headers = {"User-Agent": "crypto-scanner-research/1.0"}
    with httpx.Client(timeout=45.0, headers=headers, follow_redirects=False) as client:
        for year, month in months():
            url, filename = _premium_url(symbol, year, month)
            data = client.get(url)
            checksum = client.get(f"{url}.CHECKSUM")
            if data.status_code != 200 or checksum.status_code != 200:
                raise RuntimeError(
                    f"PREMIUM_ARCHIVE_FETCH_FAILED:{symbol}:{year:04d}-{month:02d}:"
                    f"data={data.status_code}:checksum={checksum.status_code}"
                )
            verify_archive(data.content, checksum.text, filename)
            premium.extend(_parse_premium(data.content))
    with BinancePublicArchiveClient(timeout_seconds=45.0) as client:
        for year, month in months():
            candles = client.fetch_month(make_monthly_package(symbol, "5m", year, month))
            for candle in candles:
                stamp = datetime.fromtimestamp(candle.start_time_ms / 1000, tz=UTC)
                if START <= stamp < END and stamp.minute == 0:
                    hourly_opens[candle.start_time_ms] = float(candle.open)
    premium.sort(key=lambda row: row[0])
    if any(b[0] <= a[0] for a, b in zip(premium, premium[1:], strict=False)):
        raise RuntimeError(f"non-increasing premium timestamps for {symbol}")
    funding, missing = fetch_funding_history(symbol)
    if missing:
        raise RuntimeError(f"missing funding months for core symbol {symbol}: {missing}")
    return symbol, tuple(premium), hourly_opens, funding
