from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, date, datetime

import httpx

from crypto_scanner.binance_public_archive import verify_archive
from crypto_scanner.funding_carry_research import FundingPoint
from crypto_scanner.run_funding_carry_research import fetch_funding_history

ARCHIVE_START = datetime(2023, 12, 1, tzinfo=UTC)
ARCHIVE_END = datetime(2026, 9, 1, tzinfo=UTC)


def months():
    cursor = date(2023, 12, 1)
    end = date(2026, 8, 1)
    while cursor <= end:
        yield cursor.year, cursor.month
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)


def archive_url(symbol: str, year: int, month: int) -> tuple[str, str]:
    filename = f"{symbol}-1m-{year:04d}-{month:02d}.zip"
    url = (
        "https://data.binance.vision/data/futures/um/monthly/klines/"
        f"{symbol}/1m/{filename}"
    )
    return url, filename


def parse_month(payload: bytes) -> tuple[list[tuple[int, float]], dict[int, float]]:
    quarter_hour: list[tuple[int, float]] = []
    opens: dict[int, float] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            if len(members) != 1:
                raise RuntimeError("1m archive must contain exactly one file")
            raw = archive.read(members[0]).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise RuntimeError("invalid 1m archive") from exc

    for row in csv.reader(io.StringIO(raw)):
        if not row or not row[0].isdigit():
            continue
        if len(row) < 11:
            raise RuntimeError("unexpected Binance 1m kline schema")
        ts = int(row[0])
        stamp = datetime.fromtimestamp(ts / 1000, tz=UTC)
        if not (ARCHIVE_START <= stamp < ARCHIVE_END):
            continue
        minute_mod = stamp.minute % 15
        if minute_mod == 0:
            quote_volume = float(row[7])
            taker_buy_quote = float(row[10])
            if quote_volume > 0:
                imbalance = (2.0 * taker_buy_quote - quote_volume) / quote_volume
                quarter_hour.append((ts, imbalance))
        elif minute_mod == 1:
            opens[ts] = float(row[1])
    return quarter_hour, opens


def load_symbol(symbol: str) -> tuple[str, tuple[tuple[int, float], ...], dict[int, float], tuple[FundingPoint, ...]]:
    rows: list[tuple[int, float]] = []
    opens: dict[int, float] = {}
    headers = {"User-Agent": "crypto-scanner-research/1.0"}
    with httpx.Client(timeout=45.0, headers=headers, follow_redirects=False) as client:
        for year, month in months():
            url, filename = archive_url(symbol, year, month)
            data = client.get(url)
            checksum = client.get(f"{url}.CHECKSUM")
            if data.status_code != 200 or checksum.status_code != 200:
                raise RuntimeError(
                    f"QH_ARCHIVE_FETCH_FAILED:{symbol}:{year:04d}-{month:02d}:"
                    f"data={data.status_code}:checksum={checksum.status_code}"
                )
            verify_archive(data.content, checksum.text, filename)
            month_rows, month_opens = parse_month(data.content)
            if set(opens).intersection(month_opens):
                raise RuntimeError(f"duplicate 1m timestamps for {symbol}")
            rows.extend(month_rows)
            opens.update(month_opens)
    rows.sort(key=lambda item: item[0])
    if any(b[0] <= a[0] for a, b in zip(rows, rows[1:], strict=False)):
        raise RuntimeError(f"non-increasing quarter-hour timestamps for {symbol}")
    funding, missing = fetch_funding_history(symbol)
    if missing:
        raise RuntimeError(f"missing funding months for core symbol {symbol}: {missing}")
    return symbol, tuple(rows), opens, funding
