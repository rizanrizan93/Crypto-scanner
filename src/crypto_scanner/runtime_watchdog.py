from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from crypto_scanner.persistence import (
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseRestClient,
)

_HEALTHY_STATUSES = frozenset({"RUNNING", "SUCCESS"})
_MAX_FUTURE_SKEW_MS = 5 * 60 * 1000


@dataclass(frozen=True, slots=True)
class ComponentPolicy:
    component: str
    max_age_seconds: int


DEFAULT_POLICIES = (
    ComponentPolicy("SCANNER_CYCLE", 90 * 60),
    ComponentPolicy("TRAJECTORY_CYCLE", 45 * 60),
    ComponentPolicy("CALIBRATION_CYCLE", 8 * 60 * 60),
    ComponentPolicy("PROMOTION_CYCLE", 2 * 60 * 60),
)


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    component: str
    healthy: bool
    reason: str
    status: str | None
    observed_at_ms: int | None
    age_seconds: float | None
    max_age_seconds: int


@dataclass(frozen=True, slots=True)
class RuntimeHealthReport:
    healthy: bool
    checked_at_ms: int
    components: tuple[ComponentHealth, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "healthy": self.healthy,
            "checked_at_ms": self.checked_at_ms,
            "components": [asdict(item) for item in self.components],
            "live_trading_locked": True,
        }


class SupabaseRuntimeWatchdogStore(SupabaseRestClient):
    def read_components(self, components: tuple[str, ...]) -> tuple[dict[str, object], ...]:
        if not components or any(
            not component or not component.replace("_", "").isalnum()
            for component in components
        ):
            raise PersistenceError("watchdog component list is invalid")
        response = self._client.get(
            f"{self.base_url}/rest/v1/heartbeats",
            params={
                "select": "component,observed_at_ms,status",
                "component": "in.(" + ",".join(components) + ")",
            },
            headers=self._headers(),
        )
        if response.is_error:
            raise PersistenceError(
                "runtime watchdog heartbeat read failed "
                f"status={response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise PersistenceError("runtime watchdog heartbeat response is invalid")
        return tuple(payload)


def evaluate_runtime_health(
    rows: tuple[dict[str, object], ...],
    *,
    now_ms: int,
    policies: tuple[ComponentPolicy, ...] = DEFAULT_POLICIES,
) -> RuntimeHealthReport:
    if now_ms < 0:
        raise PersistenceError("watchdog timestamp must be non-negative")
    latest: dict[str, tuple[int, str]] = {}
    for row in rows:
        try:
            component = str(row["component"]).strip().upper()
            observed_at_ms = int(row["observed_at_ms"])
            status = str(row["status"]).strip().upper()
        except (KeyError, TypeError, ValueError) as exc:
            raise PersistenceError("runtime watchdog heartbeat row is malformed") from exc
        current = latest.get(component)
        if current is None or observed_at_ms > current[0]:
            latest[component] = (observed_at_ms, status)

    results: list[ComponentHealth] = []
    for policy in policies:
        observed = latest.get(policy.component)
        if observed is None:
            results.append(
                ComponentHealth(
                    policy.component,
                    False,
                    "MISSING_HEARTBEAT",
                    None,
                    None,
                    None,
                    policy.max_age_seconds,
                )
            )
            continue
        observed_at_ms, status = observed
        age_ms = now_ms - observed_at_ms
        age_seconds = age_ms / 1000
        if age_ms < -_MAX_FUTURE_SKEW_MS:
            healthy, reason = False, "FUTURE_HEARTBEAT"
        elif status not in _HEALTHY_STATUSES:
            healthy, reason = False, f"UNHEALTHY_STATUS_{status or 'EMPTY'}"
        elif age_ms > policy.max_age_seconds * 1000:
            healthy, reason = False, "STALE_HEARTBEAT"
        else:
            healthy, reason = True, "HEALTHY"
        results.append(
            ComponentHealth(
                policy.component,
                healthy,
                reason,
                status,
                observed_at_ms,
                age_seconds,
                policy.max_age_seconds,
            )
        )
    return RuntimeHealthReport(
        healthy=all(item.healthy for item in results),
        checked_at_ms=now_ms,
        components=tuple(results),
    )


def run_watchdog(*, now_ms: int | None = None) -> RuntimeHealthReport:
    config = SupabasePersistenceConfig.from_environment()
    if not config.enabled:
        raise PersistenceError("runtime watchdog requires dedicated Crypto Scanner Supabase")
    with SupabaseRuntimeWatchdogStore(config) as store:
        rows = store.read_components(tuple(policy.component for policy in DEFAULT_POLICIES))
    return evaluate_runtime_health(
        rows,
        now_ms=time.time_ns() // 1_000_000 if now_ms is None else now_ms,
    )


def main() -> None:
    report = run_watchdog()
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    if not report.healthy:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
