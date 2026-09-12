"""Runner-local degradation latch, never a strategy cache or entry authorization."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from crypto_scanner.persistence import PersistenceError

_memory_count = 0


def _path() -> Path | None:
    root = os.getenv("RUNNER_TEMP")
    run = os.getenv("GITHUB_RUN_ID")
    if not root or not run:
        return None
    if not run.isdigit():
        raise PersistenceError("invalid management health run identity")
    return Path(root) / f"crypto-management-health-{run}.json"


def failure_count() -> int:
    path = _path()
    if path is None:
        return _memory_count
    if not path.exists():
        return 0
    try:
        value = json.loads(path.read_text())
        count = value["consecutive_failures"]
        if value["schema_version"] != 1 or type(count) is not int or count < 0:
            raise ValueError("invalid health state")
        return count
    except (ValueError, KeyError, TypeError) as exc:
        raise PersistenceError("management health state is malformed; entry blocked") from exc


def record_tick(*, degraded: bool) -> dict[str, object]:
    global _memory_count
    previous = failure_count()
    count = previous + 1 if degraded else 0
    payload = {
        "schema_version": 1,
        "consecutive_failures": count,
        "observed_at_ms": time.time_ns() // 1_000_000,
        "status": "DEGRADED" if degraded else "RECOVERED" if previous else "RUNNING",
        "sustained_outage": count >= 3,
        "new_execution_blocked": degraded,
    }
    path = _path()
    if path is not None:
        fd, name = tempfile.mkstemp(prefix="crypto-health-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(payload, stream)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    _memory_count = count
    return payload
