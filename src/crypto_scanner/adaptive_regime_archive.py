from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass

from crypto_scanner.binance_public_archive import verify_archive


@dataclass(frozen=True, slots=True)
class Bar:
    start_time_ms: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class FundingPoint:
    time_ms: int
    rate: float


def norm_ts(value: int) -> int:
    return value // 1000 if value > 100_000_000_000_000 else value


def verified(payload: bytes, checksum: str, filename: str) -> bytes:
    verify_archive(payload, checksum, filename)
    return payload


def parse_bars(payload: bytes) -> list[Bar]:
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        members = [m for m in z.infolist() if not m.is_dir()]
        if len(members) != 1:
            raise RuntimeError("kline archive shape")
        raw = z.read(members[0]).decode("utf-8")
    out = []
    for row in csv.reader(io.StringIO(raw)):
        if row and row[0].isdigit():
            out.append(Bar(norm_ts(int(row[0])), float(row[1]), float(row[2]), float(row[3]), float(row[4])))
    return out


def parse_funding(payload: bytes) -> list[FundingPoint]:
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        members = [m for m in z.infolist() if not m.is_dir()]
        if len(members) != 1:
            raise RuntimeError("funding archive shape")
        raw = z.read(members[0]).decode("utf-8")
    out = []
    for row in csv.DictReader(io.StringIO(raw)):
        if row.get("calc_time"):
            out.append(FundingPoint(norm_ts(int(row["calc_time"])), float(row["last_funding_rate"])))
    return out
