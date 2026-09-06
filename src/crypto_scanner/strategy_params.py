from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from crypto_scanner.persistence import PersistenceError, SupabasePersistenceConfig

STRATEGY_STATE_KEY = "strategy_parameters_v1"
STRATEGY_CONFIG_VERSION = "strategy-calibration-v1"


@dataclass(frozen=True, slots=True)
class StrategyParameters:
    """Bounded, evidence-calibratable parameters for entry/SL/TP behavior.

    Bounds intentionally never relax the original hard quality floors:
    TP1 remains >=1.20R, TP2 remains >=2.00R, chase can only tighten from 0.80 ATR,
    and stop-buffer changes remain narrowly bounded around the original 0.15 ATR.
    """

    stop_buffer_atr: Decimal = Decimal("0.15")
    max_chase_atr: Decimal = Decimal("0.80")
    min_rr_tp1: Decimal = Decimal("1.20")
    min_rr_tp2: Decimal = Decimal("2.00")
    tp2_cap_rr: Decimal | None = None

    def validate(self) -> None:
        if not Decimal("0.12") <= self.stop_buffer_atr <= Decimal("0.20"):
            raise ValueError("stop_buffer_atr must remain between 0.12 and 0.20")
        if not Decimal("0.60") <= self.max_chase_atr <= Decimal("0.80"):
            raise ValueError("max_chase_atr must remain between 0.60 and 0.80")
        if not Decimal("1.20") <= self.min_rr_tp1 <= Decimal("1.50"):
            raise ValueError("min_rr_tp1 must remain between 1.20 and 1.50")
        if not Decimal("2.00") <= self.min_rr_tp2 <= Decimal("2.50"):
            raise ValueError("min_rr_tp2 must remain between 2.00 and 2.50")
        if self.tp2_cap_rr is not None and not (
            Decimal("2.00") <= self.tp2_cap_rr <= Decimal("3.00")
        ):
            raise ValueError("tp2_cap_rr must remain between 2.00 and 3.00 when enabled")

    def to_dict(self) -> dict[str, str | None]:
        self.validate()
        return {
            "stop_buffer_atr": str(self.stop_buffer_atr),
            "max_chase_atr": str(self.max_chase_atr),
            "min_rr_tp1": str(self.min_rr_tp1),
            "min_rr_tp2": str(self.min_rr_tp2),
            "tp2_cap_rr": str(self.tp2_cap_rr) if self.tp2_cap_rr is not None else None,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> StrategyParameters:
        if not isinstance(value, dict):
            raise ValueError("strategy parameter state must be an object")
        params = value.get("params", value)
        if not isinstance(params, dict):
            raise ValueError("strategy params must be an object")

        def dec(name: str, default: str) -> Decimal:
            raw = params.get(name, default)
            return Decimal(str(raw))

        cap_raw = params.get("tp2_cap_rr")
        result = cls(
            stop_buffer_atr=dec("stop_buffer_atr", "0.15"),
            max_chase_atr=dec("max_chase_atr", "0.80"),
            min_rr_tp1=dec("min_rr_tp1", "1.20"),
            min_rr_tp2=dec("min_rr_tp2", "2.00"),
            tp2_cap_rr=(Decimal(str(cap_raw)) if cap_raw not in {None, ""} else None),
        )
        result.validate()
        return result


DEFAULT_STRATEGY_PARAMETERS = StrategyParameters()


def load_strategy_parameters(
    config: SupabasePersistenceConfig,
    *,
    client: httpx.Client | None = None,
) -> StrategyParameters:
    """Load the latest calibrated strategy state; missing state means safe defaults.

    A malformed persisted state is not ignored. It raises and therefore prevents an armed
    scanner cycle from silently trading with ambiguous calibration parameters.
    """

    config.validate()
    if not config.enabled:
        return DEFAULT_STRATEGY_PARAMETERS
    assert config.url is not None and config.service_role_key is not None
    owns_client = client is None
    http = client or httpx.Client(timeout=10.0)
    try:
        response = http.get(
            f"{config.url.rstrip('/')}/rest/v1/runtime_state",
            params={
                "select": "state",
                "state_key": f"eq.{STRATEGY_STATE_KEY}",
                "limit": "1",
            },
            headers={
                "apikey": config.service_role_key,
                "Authorization": f"Bearer {config.service_role_key}",
            },
        )
        if response.is_error:
            raise PersistenceError(
                "strategy state read failed "
                f"status={response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise PersistenceError("strategy state response is invalid")
        if not payload:
            return DEFAULT_STRATEGY_PARAMETERS
        row = payload[0]
        if not isinstance(row, dict) or "state" not in row:
            raise PersistenceError("strategy state row is invalid")
        try:
            return StrategyParameters.from_mapping(row["state"])
        except (ValueError, ArithmeticError) as exc:
            raise PersistenceError(f"strategy state is malformed: {exc}") from exc
    finally:
        if owns_client:
            http.close()
