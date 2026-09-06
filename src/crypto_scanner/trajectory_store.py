from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from crypto_scanner.trajectory import TrajectoryMetrics


@dataclass(frozen=True, slots=True)
class TrajectoryRecord:
    snapshot: TrajectoryMetrics
    calibration_eligible: bool
    persistence_mode: str
    note: str


class TrajectoryStore(Protocol):
    def save(self, records: tuple[TrajectoryRecord, ...]) -> None: ...


class NullTrajectoryStore:
    def save(self, records: tuple[TrajectoryRecord, ...]) -> None:
        del records


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def record_to_dict(record: TrajectoryRecord) -> dict[str, object]:
    payload = asdict(record)
    snapshot = payload["snapshot"]
    if isinstance(snapshot, dict):
        direction = snapshot.get("direction")
        quality = snapshot.get("quality")
        if hasattr(direction, "value"):
            snapshot["direction"] = direction.value
        if hasattr(quality, "value"):
            snapshot["quality"] = quality.value
    return payload


class JsonTrajectoryStore:
    """Diagnostic file sink until the dedicated Crypto Scanner Supabase is connected."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, records: tuple[TrajectoryRecord, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [record_to_dict(record) for record in records]
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n",
            encoding="utf-8",
        )
