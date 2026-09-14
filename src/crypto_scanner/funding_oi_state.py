from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import httpx

from crypto_scanner.persistence import (
    PersistenceError,
    SupabasePersistenceConfig,
    SupabaseRestClient,
    read_with_retry,
)

FUNDING_OI_STATE_KEY = "funding_oi_daily_snapshot_v1"
FUNDING_OI_STATE_SCHEMA = "funding-oi-daily-snapshot-v1"


@dataclass(frozen=True, slots=True)
class FundingOiSnapshotState:
    version: int
    current_day: date
    current_values: dict[str, Decimal]
    previous_day: date | None
    previous_values: dict[str, Decimal]

    def growth(self) -> dict[str, Decimal]:
        if self.previous_day is None:
            return {}
        if self.current_day - self.previous_day != timedelta(days=1):
            return {}
        result: dict[str, Decimal] = {}
        for symbol, current in self.current_values.items():
            previous = self.previous_values.get(symbol)
            if previous is None or previous <= 0 or current <= 0:
                continue
            result[symbol] = current / previous - Decimal(1)
        return result

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": FUNDING_OI_STATE_SCHEMA,
            "current_day": self.current_day.isoformat(),
            "current_values": {
                symbol: str(value) for symbol, value in sorted(self.current_values.items())
            },
            "previous_day": self.previous_day.isoformat() if self.previous_day else None,
            "previous_values": {
                symbol: str(value) for symbol, value in sorted(self.previous_values.items())
            },
        }

    @classmethod
    def from_row(cls, row: dict[str, object]) -> FundingOiSnapshotState:
        version = row.get("version")
        raw = row.get("state")
        if not isinstance(version, int) or version < 1 or not isinstance(raw, dict):
            raise PersistenceError("Funding+OI snapshot state row is invalid")
        if raw.get("schema_version") != FUNDING_OI_STATE_SCHEMA:
            raise PersistenceError("Funding+OI snapshot schema is invalid")
        current_day_raw = raw.get("current_day")
        if not isinstance(current_day_raw, str):
            raise PersistenceError("Funding+OI current_day is invalid")
        previous_day_raw = raw.get("previous_day")
        current_values = _parse_values(raw.get("current_values"))
        previous_values = _parse_values(raw.get("previous_values"))
        return cls(
            version=version,
            current_day=date.fromisoformat(current_day_raw),
            current_values=current_values,
            previous_day=(
                date.fromisoformat(previous_day_raw)
                if isinstance(previous_day_raw, str)
                else None
            ),
            previous_values=previous_values,
        )


def _parse_values(value: object) -> dict[str, Decimal]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Decimal] = {}
    for raw_symbol, raw_value in value.items():
        symbol = str(raw_symbol).upper()
        try:
            parsed = Decimal(str(raw_value))
        except (ValueError, ArithmeticError) as exc:
            raise PersistenceError("Funding+OI snapshot contains invalid decimal") from exc
        if parsed > 0:
            result[symbol] = parsed
    return result


class _FundingOiStateRest(SupabaseRestClient):
    def select_state(self) -> FundingOiSnapshotState | None:
        response = read_with_retry(
            self._client,
            f"{self.base_url}/rest/v1/runtime_state",
            params={
                "select": "version,state",
                "state_key": f"eq.{FUNDING_OI_STATE_KEY}",
                "limit": "1",
            },
            operation="FUNDING_OI_STATE",
            headers=self._headers(),
        )
        if response.is_error:
            raise PersistenceError(
                f"Funding+OI state read failed status={response.status_code}"
            )
        payload = response.json()
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise PersistenceError("Funding+OI state response is invalid")
        if not payload:
            return None
        return FundingOiSnapshotState.from_row(payload[0])

    def save_state(self, state: FundingOiSnapshotState, *, updated_at_ms: int) -> None:
        self.upsert(
            "runtime_state",
            (
                {
                    "state_key": FUNDING_OI_STATE_KEY,
                    "version": state.version,
                    "state": state.to_dict(),
                    "updated_at_ms": updated_at_ms,
                },
            ),
            on_conflict=("state_key",),
        )


def roll_daily_oi_snapshot(
    config: SupabasePersistenceConfig,
    *,
    current_day: date,
    values: dict[str, Decimal],
    updated_at_ms: int,
    client: httpx.Client | None = None,
) -> FundingOiSnapshotState:
    if not values:
        raise PersistenceError("Funding+OI daily snapshot requires positive OI values")
    normalized = {symbol.upper(): value for symbol, value in values.items() if value > 0}
    if not normalized:
        raise PersistenceError("Funding+OI daily snapshot has no usable OI values")

    with _FundingOiStateRest(config, client=client) as rest:
        current = rest.select_state()
        if current is None:
            state = FundingOiSnapshotState(1, current_day, normalized, None, {})
            rest.save_state(state, updated_at_ms=updated_at_ms)
            return state
        if current_day < current.current_day:
            raise PersistenceError("Funding+OI snapshot clock moved backwards")
        if current_day == current.current_day:
            # Freeze the first post-midnight observation for forward reproducibility.
            return current
        state = FundingOiSnapshotState(
            version=current.version + 1,
            current_day=current_day,
            current_values=normalized,
            previous_day=current.current_day,
            previous_values=current.current_values,
        )
        rest.save_state(state, updated_at_ms=updated_at_ms)
        return state
