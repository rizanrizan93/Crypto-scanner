from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass

import httpx

from crypto_scanner.persistence import (
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseRestClient,
)

_ALLOWED_STATUSES = frozenset({"RUNNING", "SUCCESS", "FAILED", "BLOCKED"})


@dataclass(frozen=True, slots=True)
class RuntimeHeartbeat:
    component: str
    observed_at_ms: int
    status: str
    git_sha: str | None
    details: dict[str, object]


class SupabaseRuntimeHealthStore:
    """Best-effort operational heartbeat persistence for scheduled runtimes."""

    def __init__(
        self,
        config: SupabasePersistenceConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._rest = SupabaseRestClient(config, client=client)

    def close(self) -> None:
        self._rest.close()

    def __enter__(self) -> SupabaseRuntimeHealthStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def record(
        self,
        component: str,
        status: str,
        *,
        git_sha: str | None = None,
        details: dict[str, object] | None = None,
        observed_at_ms: int | None = None,
    ) -> RuntimeHeartbeat:
        normalized_component = component.strip().upper()
        normalized_status = status.strip().upper()
        if not normalized_component or not normalized_component.replace("_", "").isalnum():
            raise PersistenceError("runtime heartbeat component must be alphanumeric/underscore")
        if normalized_status not in _ALLOWED_STATUSES:
            raise PersistenceError(
                "runtime heartbeat status must be RUNNING, SUCCESS, FAILED, or BLOCKED"
            )
        observed = observed_at_ms if observed_at_ms is not None else time.time_ns() // 1_000_000
        if observed < 0:
            raise PersistenceError("runtime heartbeat timestamp must be non-negative")
        heartbeat = RuntimeHeartbeat(
            component=normalized_component,
            observed_at_ms=observed,
            status=normalized_status,
            git_sha=git_sha.strip() if git_sha and git_sha.strip() else None,
            details=dict(details or {}),
        )
        self._rest.upsert(
            "heartbeats",
            (
                {
                    "component": heartbeat.component,
                    "observed_at_ms": heartbeat.observed_at_ms,
                    "status": heartbeat.status,
                    "git_sha": heartbeat.git_sha,
                    "details": heartbeat.details,
                },
            ),
            on_conflict=("component",),
        )
        return heartbeat


def _github_details() -> dict[str, object]:
    values = {
        "workflow": os.getenv("GITHUB_WORKFLOW", "").strip(),
        "run_id": os.getenv("GITHUB_RUN_ID", "").strip(),
        "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT", "").strip(),
        "event": os.getenv("GITHUB_EVENT_NAME", "").strip(),
        "ref": os.getenv("GITHUB_REF_NAME", "").strip(),
    }
    return {key: value for key, value in values.items() if value}


def main() -> None:
    parser = argparse.ArgumentParser(description="Persist Crypto Scanner runtime heartbeat")
    parser.add_argument("component")
    parser.add_argument("status", choices=sorted(_ALLOWED_STATUSES))
    args = parser.parse_args()

    config = SupabasePersistenceConfig.from_environment()
    if not config.enabled:
        raise SystemExit("Crypto Scanner Supabase persistence is required for heartbeats")

    with SupabaseRuntimeHealthStore(config) as store:
        heartbeat = store.record(
            args.component,
            args.status,
            git_sha=os.getenv("GITHUB_SHA"),
            details=_github_details(),
        )
    print(
        json.dumps(
            {
                "component": heartbeat.component,
                "observed_at_ms": heartbeat.observed_at_ms,
                "status": heartbeat.status,
                "git_sha": heartbeat.git_sha,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
