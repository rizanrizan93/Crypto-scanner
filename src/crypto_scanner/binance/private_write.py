from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

import httpx

from crypto_scanner.binance.auth import BinanceDemoCredentials, encode_query, sign_query
from crypto_scanner.execution_plan import EntryOrderPlan, TestnetExecutionArm
from crypto_scanner.safety import BINANCE_DEMO_REST_URL, assert_binance_demo_url


class SubmissionState(StrEnum):
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"


class BinanceOrderSubmissionError(RuntimeError):
    """Definitive rejection received from the Binance Futures test endpoint."""


class UnknownSubmissionOutcome(RuntimeError):
    """Transport outcome is unknown; reconcile identity before any retry."""

    def __init__(self, client_id: str, message: str) -> None:
        super().__init__(message)
        self.client_id = client_id


@dataclass(frozen=True, slots=True)
class OrderSubmissionAck:
    order_id: str
    client_order_id: str
    state: SubmissionState
    exchange_time_ms: int | None


@dataclass(frozen=True, slots=True)
class AlgoSubmissionAck:
    algo_id: str
    client_algo_id: str
    state: SubmissionState
    exchange_time_ms: int | None


@dataclass(frozen=True, slots=True)
class ProtectionPlan:
    symbol: str
    exit_side: str
    qty: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    stop_client_algo_id: str
    take_profit_client_algo_id: str


def _algo_client_id(plan: EntryOrderPlan, purpose: str) -> str:
    seed = f"{plan.order_link_id}:{purpose}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    value = f"cs-{purpose}-{digest}"
    if len(value) > 32:
        raise BinanceOrderSubmissionError("generated clientAlgoId exceeds scanner safety limit")
    return value


def build_protection_plan(plan: EntryOrderPlan, filled_qty: Decimal) -> ProtectionPlan:
    if plan.side not in {"Buy", "Sell"}:
        raise BinanceOrderSubmissionError("planned entry side is invalid")
    if filled_qty <= 0:
        raise BinanceOrderSubmissionError("filled quantity must be positive before protection")
    if filled_qty > plan.qty:
        raise BinanceOrderSubmissionError("filled quantity exceeds planned entry quantity")
    exit_side = "SELL" if plan.side == "Buy" else "BUY"
    return ProtectionPlan(
        symbol=plan.symbol,
        exit_side=exit_side,
        qty=filled_qty,
        stop_loss=plan.stop_loss,
        take_profit=plan.take_profit_2,
        stop_client_algo_id=_algo_client_id(plan, "sl"),
        take_profit_client_algo_id=_algo_client_id(plan, "tp2"),
    )


class BinanceTestnetOrderClient:
    """Test-environment write gateway with deterministic IDs and no blind retries."""

    def __init__(
        self,
        credentials: BinanceDemoCredentials,
        arm: TestnetExecutionArm,
        *,
        base_url: str = BINANCE_DEMO_REST_URL,
        recv_window_ms: int = 5000,
        timeout_seconds: float = 8.0,
        client: httpx.Client | None = None,
        time_source_ms: Callable[[], int] | None = None,
    ) -> None:
        credentials.validate()
        if not 1 <= recv_window_ms <= 60000:
            raise ValueError("recv_window_ms must be between 1 and 60000")
        self._credentials = credentials
        self._arm = arm
        self.base_url = assert_binance_demo_url(base_url).rstrip("/")
        self.recv_window_ms = recv_window_ms
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._time_source_ms = time_source_ms or (lambda: time.time_ns() // 1_000_000)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BinanceTestnetOrderClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _signed_post(self, path: str, params: dict[str, object], client_id: str) -> Any:
        self._arm.require_enabled()
        timestamp_ms = self._time_source_ms()
        signed = dict(params)
        signed["recvWindow"] = self.recv_window_ms
        signed["timestamp"] = timestamp_ms
        query = encode_query(signed)
        signature = sign_query(self._credentials.api_secret, query)
        body = f"{query}&signature={signature}"
        url = f"{self.base_url}{path}"
        assert_binance_demo_url(url)
        headers = {
            "X-MBX-APIKEY": self._credentials.api_key,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            response = self._client.post(url, headers=headers, content=body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UnknownSubmissionOutcome(
                client_id,
                "Binance test submission transport outcome is unknown; reconcile client id "
                "before any retry",
            ) from exc
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise UnknownSubmissionOutcome(
                client_id,
                "Binance returned an undecodable write response; reconcile before retry",
            ) from exc
        if response.is_error:
            if isinstance(payload, dict):
                raise BinanceOrderSubmissionError(
                    f"Binance write rejected code={payload.get('code')} msg={payload.get('msg')}"
                )
            response.raise_for_status()
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("code"), int)
            and payload["code"] < 0
        ):
            raise BinanceOrderSubmissionError(
                f"Binance write rejected code={payload.get('code')} msg={payload.get('msg')}"
            )
        return payload

    def submit_entry(self, plan: EntryOrderPlan) -> OrderSubmissionAck:
        if plan.side not in {"Buy", "Sell"}:
            raise BinanceOrderSubmissionError("planned entry side is invalid")
        side = "BUY" if plan.side == "Buy" else "SELL"
        payload = self._signed_post(
            "/fapi/v1/order",
            {
                "symbol": plan.symbol,
                "side": side,
                "type": "MARKET",
                "positionSide": "BOTH",
                "quantity": plan.qty,
                "newClientOrderId": plan.order_link_id,
                "newOrderRespType": "ACK",
            },
            plan.order_link_id,
        )
        if not isinstance(payload, dict):
            raise UnknownSubmissionOutcome(
                plan.order_link_id,
                "Binance entry acknowledgement has unexpected shape; reconcile before retry",
            )
        order_id = str(payload.get("orderId") or "")
        returned_id = str(payload.get("clientOrderId") or "")
        if not order_id or returned_id != plan.order_link_id:
            raise UnknownSubmissionOutcome(
                plan.order_link_id,
                "Binance entry acknowledgement identity is incomplete or mismatched",
            )
        update_time = payload.get("updateTime") or payload.get("transactTime")
        return OrderSubmissionAck(
            order_id=order_id,
            client_order_id=returned_id,
            state=SubmissionState.PENDING_RECONCILIATION,
            exchange_time_ms=int(update_time) if update_time not in (None, "") else None,
        )

    def _submit_algo(
        self,
        protection: ProtectionPlan,
        *,
        order_type: str,
        trigger_price: Decimal,
        client_algo_id: str,
    ) -> AlgoSubmissionAck:
        payload = self._signed_post(
            "/fapi/v1/algoOrder",
            {
                "algoType": "CONDITIONAL",
                "symbol": protection.symbol,
                "side": protection.exit_side,
                "type": order_type,
                "positionSide": "BOTH",
                "quantity": protection.qty,
                "triggerPrice": trigger_price,
                "workingType": "MARK_PRICE",
                "reduceOnly": True,
                "clientAlgoId": client_algo_id,
                "newOrderRespType": "ACK",
            },
            client_algo_id,
        )
        if not isinstance(payload, dict):
            raise UnknownSubmissionOutcome(
                client_algo_id,
                "Binance algo acknowledgement has unexpected shape; reconcile before retry",
            )
        algo_id = str(payload.get("algoId") or "")
        returned_id = str(payload.get("clientAlgoId") or "")
        if not algo_id or returned_id != client_algo_id:
            raise UnknownSubmissionOutcome(
                client_algo_id,
                "Binance algo acknowledgement identity is incomplete or mismatched",
            )
        update_time = payload.get("updateTime") or payload.get("createTime")
        return AlgoSubmissionAck(
            algo_id=algo_id,
            client_algo_id=returned_id,
            state=SubmissionState.PENDING_RECONCILIATION,
            exchange_time_ms=int(update_time) if update_time not in (None, "") else None,
        )

    def submit_stop_loss(self, protection: ProtectionPlan) -> AlgoSubmissionAck:
        return self._submit_algo(
            protection,
            order_type="STOP_MARKET",
            trigger_price=protection.stop_loss,
            client_algo_id=protection.stop_client_algo_id,
        )

    def submit_take_profit(self, protection: ProtectionPlan) -> AlgoSubmissionAck:
        return self._submit_algo(
            protection,
            order_type="TAKE_PROFIT_MARKET",
            trigger_price=protection.take_profit,
            client_algo_id=protection.take_profit_client_algo_id,
        )
