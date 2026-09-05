from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from crypto_scanner.bybit.auth import (
    BybitTestnetCredentials,
    sign_post_request,
)
from crypto_scanner.execution_plan import EntryOrderPlan, TestnetExecutionArm
from crypto_scanner.safety import BYBIT_TESTNET_REST_URL, assert_testnet_url


class SubmissionState(StrEnum):
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"


class BybitOrderSubmissionError(RuntimeError):
    """Definitive rejection received from the Testnet order endpoint."""


class UnknownSubmissionOutcome(RuntimeError):
    """Transport outcome is unknown; caller must reconcile and must not blindly retry."""

    def __init__(self, order_link_id: str, message: str) -> None:
        super().__init__(message)
        self.order_link_id = order_link_id


@dataclass(frozen=True, slots=True)
class OrderSubmissionAck:
    order_id: str
    order_link_id: str
    state: SubmissionState
    exchange_time_ms: int | None


class BybitTestnetOrderClient:
    """Minimal Testnet-only write client. No retries and no LIVE endpoint support."""

    def __init__(
        self,
        credentials: BybitTestnetCredentials,
        arm: TestnetExecutionArm,
        *,
        base_url: str = BYBIT_TESTNET_REST_URL,
        recv_window_ms: int = 5000,
        timeout_seconds: float = 8.0,
        client: httpx.Client | None = None,
        time_source_ms: Callable[[], int] | None = None,
    ) -> None:
        credentials.validate()
        if not 1 <= recv_window_ms <= 5000:
            raise ValueError("recv_window_ms must be between 1 and 5000")
        self._credentials = credentials
        self._arm = arm
        self.base_url = assert_testnet_url(base_url).rstrip("/")
        self.recv_window_ms = recv_window_ms
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._time_source_ms = time_source_ms or (lambda: time.time_ns() // 1_000_000)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BybitTestnetOrderClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _request_payload(plan: EntryOrderPlan) -> dict[str, object]:
        return {
            "category": "linear",
            "symbol": plan.symbol,
            "side": plan.side,
            "orderType": "Market",
            "qty": str(plan.qty),
            "timeInForce": "IOC",
            "positionIdx": 0,
            "orderLinkId": plan.order_link_id,
            "reduceOnly": False,
            "takeProfit": str(plan.take_profit_2),
            "stopLoss": str(plan.stop_loss),
            "tpTriggerBy": "MarkPrice",
            "slTriggerBy": "MarkPrice",
            "tpslMode": "Full",
            "tpOrderType": "Market",
            "slOrderType": "Market",
        }

    def submit_entry(self, plan: EntryOrderPlan) -> OrderSubmissionAck:
        self._arm.require_enabled()
        path = "/v5/order/create"
        url = f"{self.base_url}{path}"
        assert_testnet_url(url)

        payload = self._request_payload(plan)
        json_body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        timestamp_ms = self._time_source_ms()
        signature = sign_post_request(
            api_key=self._credentials.api_key,
            api_secret=self._credentials.api_secret,
            timestamp_ms=timestamp_ms,
            recv_window_ms=self.recv_window_ms,
            json_body=json_body,
        )
        headers = {
            "X-BAPI-API-KEY": self._credentials.api_key,
            "X-BAPI-TIMESTAMP": str(timestamp_ms),
            "X-BAPI-SIGN": signature,
            "X-BAPI-RECV-WINDOW": str(self.recv_window_ms),
            "Content-Type": "application/json",
        }

        try:
            response = self._client.post(url, headers=headers, content=json_body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UnknownSubmissionOutcome(
                plan.order_link_id,
                "Bybit Testnet submission transport outcome is unknown; reconcile by orderLinkId "
                "before any further action",
            ) from exc

        if response.status_code == 403:
            raise BybitOrderSubmissionError(
                "Bybit Testnet order endpoint returned HTTP 403; execution remains blocked"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BybitOrderSubmissionError(
                f"Bybit Testnet order endpoint returned HTTP {response.status_code}"
            ) from exc

        try:
            decoded: Any = response.json()
        except ValueError as exc:
            raise UnknownSubmissionOutcome(
                plan.order_link_id,
                "Bybit returned an undecodable order response; reconcile by orderLinkId",
            ) from exc
        if not isinstance(decoded, dict):
            raise UnknownSubmissionOutcome(
                plan.order_link_id,
                "Bybit returned an unexpected order response shape; reconcile by orderLinkId",
            )
        if decoded.get("retCode") != 0:
            raise BybitOrderSubmissionError(
                "Bybit order rejected "
                f"retCode={decoded.get('retCode')} retMsg={decoded.get('retMsg')}"
            )
        result = decoded.get("result")
        if not isinstance(result, dict):
            raise UnknownSubmissionOutcome(
                plan.order_link_id,
                "Bybit accepted response without a result object; reconcile by orderLinkId",
            )

        order_id = str(result.get("orderId") or "")
        returned_link_id = str(result.get("orderLinkId") or "")
        if not order_id or returned_link_id != plan.order_link_id:
            raise UnknownSubmissionOutcome(
                plan.order_link_id,
                "Bybit acknowledgement identity is incomplete or mismatched; "
                "reconcile before retry",
            )
        exchange_time = decoded.get("time")
        return OrderSubmissionAck(
            order_id=order_id,
            order_link_id=returned_link_id,
            state=SubmissionState.PENDING_RECONCILIATION,
            exchange_time_ms=int(exchange_time) if exchange_time not in (None, "") else None,
        )
