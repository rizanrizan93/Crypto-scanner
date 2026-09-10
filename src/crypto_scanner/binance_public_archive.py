from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from crypto_scanner.binance.models import Candle

_ARCHIVE_BASE = "https://data.binance.vision/data/futures/um"
_ALLOWED_INTERVALS = {"1m", "3m", "5m", "15m", "1h", "4h"}
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024


class BinanceArchiveError(RuntimeError):
    """Raised when a public Binance archive package is invalid or unverifiable."""


@dataclass(frozen=True, slots=True)
class ArchivePackage:
    symbol: str
    interval: str
    year: int
    month: int

    @property
    def filename(self) -> str:
        return f"{self.symbol}-{self.interval}-{self.year:04d}-{self.month:02d}.zip"

    @property
    def url(self) -> str:
        return (
            f"{_ARCHIVE_BASE}/monthly/klines/{self.symbol}/{self.interval}/"
            f"{self.filename}"
        )

    @property
    def checksum_url(self) -> str:
        return f"{self.url}.CHECKSUM"


def make_monthly_package(
    symbol: str,
    interval: str,
    year: int,
    month: int,
) -> ArchivePackage:
    normalized = symbol.upper().strip()
    if not normalized.endswith("USDT") or not normalized.isalnum():
        raise ValueError("research archive symbol must be an alphanumeric USD-M USDT symbol")
    if interval not in _ALLOWED_INTERVALS:
        raise ValueError(f"unsupported archive interval: {interval}")
    if year < 2020 or not 1 <= month <= 12:
        raise ValueError("archive date is outside the supported bounded range")
    return ArchivePackage(normalized, interval, year, month)


def _parse_checksum(text: str, filename: str) -> str:
    parts = text.strip().split()
    if len(parts) < 2 or parts[-1].lstrip("*") != filename:
        raise BinanceArchiveError("checksum manifest does not match archive filename")
    digest = parts[0].lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise BinanceArchiveError("checksum manifest has invalid SHA-256")
    return digest


def verify_archive(payload: bytes, checksum_text: str, filename: str) -> None:
    if len(payload) > _MAX_ARCHIVE_BYTES:
        raise BinanceArchiveError("archive exceeds compressed-size guard")
    expected = _parse_checksum(checksum_text, filename)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise BinanceArchiveError("archive SHA-256 mismatch")


def parse_kline_zip(payload: bytes) -> tuple[Candle, ...]:
    if len(payload) > _MAX_ARCHIVE_BYTES:
        raise BinanceArchiveError("archive exceeds compressed-size guard")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1:
                raise BinanceArchiveError("kline archive must contain exactly one data file")
            member = members[0]
            if member.file_size > _MAX_UNCOMPRESSED_BYTES:
                raise BinanceArchiveError("archive exceeds uncompressed-size guard")
            raw = archive.read(member).decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise BinanceArchiveError("invalid kline archive") from exc

    candles: list[Candle] = []
    reader = csv.reader(io.StringIO(raw))
    try:
        for row in reader:
            if not row:
                continue
            if not row[0].isdigit():
                continue
            if len(row) < 8:
                raise BinanceArchiveError("kline row has unexpected shape")
            candles.append(
                Candle(
                    start_time_ms=int(row[0]),
                    open=Decimal(row[1]),
                    high=Decimal(row[2]),
                    low=Decimal(row[3]),
                    close=Decimal(row[4]),
                    volume=Decimal(row[5]),
                    turnover=Decimal(row[7]),
                )
            )
    except (InvalidOperation, ValueError) as exc:
        raise BinanceArchiveError("kline row contains invalid numeric data") from exc

    if not candles:
        raise BinanceArchiveError("kline archive contains no candles")
    if any(
        b.start_time_ms <= a.start_time_ms
        for a, b in zip(candles, candles[1:], strict=False)
    ):
        raise BinanceArchiveError("kline archive timestamps are not strictly increasing")
    return tuple(candles)


class BinancePublicArchiveClient:
    """Read-only public market-data client for research; never touches account endpoints."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BinancePublicArchiveClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fetch_month(self, package: ArchivePackage) -> tuple[Candle, ...]:
        data_response = self._client.get(package.url)
        checksum_response = self._client.get(package.checksum_url)
        if data_response.status_code != 200 or checksum_response.status_code != 200:
            raise BinanceArchiveError(
                "archive fetch failed "
                f"data={data_response.status_code} "
                f"checksum={checksum_response.status_code}"
            )
        verify_archive(data_response.content, checksum_response.text, package.filename)
        return parse_kline_zip(data_response.content)
