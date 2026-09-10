from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from crypto_scanner.binance_public_archive import (
    BinanceArchiveError,
    make_monthly_package,
    parse_kline_zip,
    verify_archive,
)


def _zip_csv(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("BTCUSDT-1m-2026-01.csv", text)
    return buffer.getvalue()


def test_monthly_package_uses_usdm_public_archive_path() -> None:
    package = make_monthly_package("btcusdt", "1m", 2026, 1)
    assert package.filename == "BTCUSDT-1m-2026-01.zip"
    assert "/data/futures/um/monthly/klines/BTCUSDT/1m/" in package.url
    assert package.checksum_url.endswith(".zip.CHECKSUM")


def test_checksum_is_fail_closed() -> None:
    payload = b"archive"
    digest = hashlib.sha256(payload).hexdigest()
    verify_archive(payload, f"{digest}  BTCUSDT-1m-2026-01.zip", "BTCUSDT-1m-2026-01.zip")
    with pytest.raises(BinanceArchiveError, match="SHA-256 mismatch"):
        verify_archive(b"changed", f"{digest}  BTCUSDT-1m-2026-01.zip", "BTCUSDT-1m-2026-01.zip")


def test_parse_kline_zip_reads_decimal_candles() -> None:
    payload = _zip_csv(
        "open_time,open,high,low,close,volume,close_time,quote_volume,trades,taker_base,taker_quote,ignore\n"
        "1767225600000,100,102,99,101,10,1767225659999,1005,4,6,603,0\n"
        "1767225660000,101,103,100,102,12,1767225719999,1224,5,7,714,0\n"
    )
    candles = parse_kline_zip(payload)
    assert len(candles) == 2
    assert str(candles[0].close) == "101"
    assert str(candles[1].turnover) == "1224"
