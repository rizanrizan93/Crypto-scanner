from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from urllib.parse import urlparse

import httpx

from crypto_scanner.trajectory_store import TrajectoryRecord, TrajectoryState

SCHEMA_VERSION = "crypto-scanner-persistence-v1"


class PersistenceError(RuntimeError):
    """Raised when durable Crypto Scanner persistence fails."""


@dataclass(frozen=True, slots=True)
class SupabasePersistenceConfig:
    url: str | None
    service_role_key: str | None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.service_role_key)

    @classmethod
    def from_environment(cls) -> SupabasePersistenceConfig:
        url = os.getenv("CRYPTO_SCANNER_SUPABASE_URL", "").strip() or None
        key = os.getenv("CRYPTO_SCANNER_SUPABASE_SERVICE_ROLE_KEY", "").strip() or None
        if bool(url) != bool(key):
            raise PersistenceError(
                "CRYPTO_SCANNER_SUPABASE_URL and "
                "CRYPTO_SCANNER_SUPABASE_SERVICE_ROLE_KEY must be configured together"
            )
        config = cls(url=url, service_role_key=key)
        config.validate()
        return config

    def validate(self) -> None:
        if not self.enabled:
            return
        assert self.url is not None
        parsed = urlparse(self.url)
        if parsed.scheme != "https":
            raise PersistenceError("Crypto Scanner Supabase URL must use HTTPS")
        if not parsed.hostname or not parsed.hostname.endswith(".supabase.co"):
            raise PersistenceError("Crypto Scanner Supabase URL must be a supabase.co project URL")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise PersistenceError(
                "Crypto Scanner Supabase URL must not contain path/query/fragment"
            )


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _without_none(row: dict[str, object]) -> dict[str, object]:
    return {key: _json_value(value) for key, value in row.items() if value is not None}


def stable_position_id(record: TrajectoryRecord) -> str:
    snapshot = record.snapshot
    raw = (
        f"BINANCE|DEMO|{snapshot.symbol}|{snapshot.direction.value}|"
        f"{snapshot.entry_time_ms}"
    ).encode()
    digest = hashlib.sha256(raw).hexdigest()[:32]
    return f"pos-{digest}"


class SupabaseRestClient:
    def __init__(
        self,
        config: SupabasePersistenceConfig,
        *,
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        config.validate()
        if not config.enabled:
            raise PersistenceError("Supabase persistence is not configured")
        assert config.url is not None and config.service_role_key is not None
        self.base_url = config.url.rstrip("/")
        self._key = config.service_role_key
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SupabaseRestClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _headers(self, *, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def upsert(
        self,
        table: str,
        rows: tuple[dict[str, object], ...],
        *,
        on_conflict: tuple[str, ...],
    ) -> None:
        if not rows:
            return
        if not table.replace("_", "").isalnum():
            raise PersistenceError("invalid persistence table name")
        if not on_conflict or any(
            not column.replace("_", "").isalnum() for column in on_conflict
        ):
            raise PersistenceError("invalid persistence conflict target")
        url = f"{self.base_url}/rest/v1/{table}"
        params = {"on_conflict": ",".join(on_conflict)}
        payload = [_without_none(row) for row in rows]
        response = self._client.post(
            url,
            params=params,
            headers=self._headers(prefer="resolution=merge-duplicates,return=minimal"),
            json=payload,
        )
        if response.is_error:
            detail = response.text[:500]
            raise PersistenceError(
                f"Supabase upsert failed table={table} status={response.status_code}: {detail}"
            )

    def schema_version(self) -> str:
        url = f"{self.base_url}/rest/v1/schema_meta"
        response = self._client.get(
            url,
            params={"select": "value", "key": "eq.schema_version", "limit": "1"},
            headers=self._headers(),
        )
        if response.is_error:
            raise PersistenceError(
                f"Supabase schema check failed status={response.status_code}: "
                f"{response.text[:500]}"
            )
        payload = response.json()
        if not isinstance(payload, list) or len(payload) != 1:
            raise PersistenceError("Supabase schema version row is missing")
        value = payload[0].get("value") if isinstance(payload[0], dict) else None
        if not isinstance(value, str):
            raise PersistenceError("Supabase schema version value is invalid")
        return value


class SupabaseTrajectoryStore:
    """Idempotent durable store for Phase 7 position and trajectory evidence."""

    def __init__(
        self,
        config: SupabasePersistenceConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._rest = SupabaseRestClient(config, client=client)

    def close(self) -> None:
        self._rest.close()

    def __enter__(self) -> SupabaseTrajectoryStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def assert_schema_current(self) -> None:
        actual = self._rest.schema_version()
        if actual != SCHEMA_VERSION:
            raise PersistenceError(
                f"Supabase schema mismatch expected={SCHEMA_VERSION} actual={actual}"
            )

    def save(self, records: tuple[TrajectoryRecord, ...]) -> None:
        if not records:
            return
        self.assert_schema_current()
        positions: dict[str, dict[str, object]] = {}
        trajectories: list[dict[str, object]] = []
        closed_trades: list[dict[str, object]] = []

        for record in records:
            snapshot = record.snapshot
            position_id = stable_position_id(record)
            position_row: dict[str, object] = {
                "position_id": position_id,
                "venue": "BINANCE",
                "environment": "DEMO",
                "symbol": snapshot.symbol,
                "direction": snapshot.direction,
                "state": record.state,
                "entry_qty": snapshot.reference_qty,
                "remaining_qty": (
                    snapshot.reference_qty if record.state is TrajectoryState.OPEN else Decimal(0)
                ),
                "average_entry_price": snapshot.entry_price,
                "initial_stop_loss": snapshot.initial_stop_loss,
                "opened_at_ms": snapshot.entry_time_ms,
                "closed_at_ms": record.exit_time_ms,
                "latest_mark_price": snapshot.current_price,
                "updated_at_ms": snapshot.measured_until_ms,
                "source": {
                    "quality": snapshot.quality,
                    "history_complete": snapshot.history_complete,
                    "persistence_mode": "SUPABASE",
                },
            }
            positions[position_id] = _without_none(position_row)

            trajectories.append(
                _without_none(
                    {
                        "position_id": position_id,
                        "measured_until_ms": snapshot.measured_until_ms,
                        "state": record.state,
                        "current_price": snapshot.current_price,
                        "reference_qty": snapshot.reference_qty,
                        "favorable_extreme_price": snapshot.favorable_extreme_price,
                        "adverse_extreme_price": snapshot.adverse_extreme_price,
                        "mfe_per_unit": snapshot.mfe_per_unit,
                        "mae_per_unit": snapshot.mae_per_unit,
                        "mfe_pct": snapshot.mfe_pct,
                        "mae_pct": snapshot.mae_pct,
                        "current_pnl_per_unit": snapshot.current_pnl_per_unit,
                        "initial_stop_loss": snapshot.initial_stop_loss,
                        "mfe_r": snapshot.mfe_r,
                        "mae_r": snapshot.mae_r,
                        "holding_time_ms": snapshot.holding_time_ms,
                        "observation_count": snapshot.observation_count,
                        "quality": snapshot.quality,
                        "history_complete": snapshot.history_complete,
                        "realized_pnl": record.realized_pnl,
                        "commission": record.commission,
                        "funding_fee": record.funding_fee,
                        "net_pnl": record.net_pnl,
                        "calibration_eligible": record.calibration_eligible,
                        "note": record.note,
                    }
                )
            )

            if record.state is TrajectoryState.CLOSED:
                if record.exit_time_ms is None or record.exit_price is None:
                    raise PersistenceError("closed trajectory is missing exit evidence")
                if (
                    record.realized_pnl is None
                    or record.commission is None
                    or record.funding_fee is None
                    or record.net_pnl is None
                ):
                    raise PersistenceError("closed trajectory is missing PnL evidence")
                closed_trades.append(
                    _without_none(
                        {
                            "trade_key": position_id,
                            "position_id": position_id,
                            "symbol": snapshot.symbol,
                            "direction": snapshot.direction,
                            "entry_time_ms": snapshot.entry_time_ms,
                            "exit_time_ms": record.exit_time_ms,
                            "entry_qty": snapshot.reference_qty,
                            "exit_qty": snapshot.reference_qty,
                            "average_entry_price": snapshot.entry_price,
                            "average_exit_price": record.exit_price,
                            "realized_pnl": record.realized_pnl,
                            "commission": record.commission,
                            "funding_fee": record.funding_fee,
                            "net_pnl": record.net_pnl,
                            "mfe_pct": snapshot.mfe_pct,
                            "mae_pct": snapshot.mae_pct,
                            "mfe_r": snapshot.mfe_r,
                            "mae_r": snapshot.mae_r,
                            "holding_time_ms": snapshot.holding_time_ms,
                            "history_complete": snapshot.history_complete,
                            "observation_count": snapshot.observation_count,
                            "calibration_eligible": record.calibration_eligible,
                        }
                    )
                )

        self._rest.upsert(
            "positions",
            tuple(positions.values()),
            on_conflict=("position_id",),
        )
        self._rest.upsert(
            "position_trajectory",
            tuple(trajectories),
            on_conflict=("position_id", "measured_until_ms"),
        )
        self._rest.upsert(
            "closed_trades",
            tuple(closed_trades),
            on_conflict=("trade_key",),
        )
