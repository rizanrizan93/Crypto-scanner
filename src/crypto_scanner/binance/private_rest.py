from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from crypto_scanner.binance.auth import BinanceDemoCredentials, encode_query, sign_query
from crypto_scanner.binance.models import (
    OrderSnapshot,
    PositionSnapshot,
    WalletCoin,
    WalletSnapshot,
    decimal_optional,
    decimal_required,
)
from crypto_scanner.safety import BINANCE_DEMO_REST_URL, assert_binance_demo_url


class BinancePrivateApiError(RuntimeError):
    """Raised when a Binance Futures test read-only request fails."""


@dataclass(frozen=True, slots=True)
class AlgoOrderSnapshot:
    algo_id: str
    client_algo_id: str
    symbol: str
    side: str
    order_type: str
    status: str
    trigger_price: Decimal | None
    quantity: Decimal | None
    reduce_only: bool
    updated_time_ms: int | None


_ALLOWED_READ_PATHS = frozenset(
    {
        "/fapi/v2/account",
        "/fapi/v2/positionRisk",
        "/fapi/v1/openOrders",
        "/fapi/v1/order",
        "/fapi/v1/algoOrder",
    }
)


def _parse_order(item: dict[str, Any]) -> OrderSnapshot:
    executed_qty = decimal_optional(item.get("executedQty"))
    orig_qty = decimal_required(item.get("origQty"), "order.origQty")
    created = item.get("time")
    updated = item.get("updateTime")
    return OrderSnapshot(
        order_id=str(item.get("orderId", "")),
        order_link_id=str(item.get("clientOrderId", "")),
        symbol=str(item.get("symbol", "")),
        side="Buy" if item.get("side") == "BUY" else "Sell",
        order_status=str(item.get("status", "")),
        order_type=str(item.get("type", "")),
        time_in_force=str(item.get("timeInForce", "")),
        price=decimal_optional(item.get("price")),
        qty=orig_qty,
        avg_price=decimal_optional(item.get("avgPrice")),
        leaves_qty=(orig_qty - executed_qty if executed_qty is not None else None),
        cum_exec_qty=executed_qty,
        cum_exec_value=decimal_optional(item.get("cumQuote")),
        cum_exec_fee=None,
        trigger_price=decimal_optional(item.get("stopPrice")),
        take_profit=None,
        stop_loss=None,
        reduce_only=bool(item.get("reduceOnly", False)),
        close_on_trigger=bool(item.get("closePosition", False)),
        created_time_ms=int(created) if created not in (None, "") else None,
        updated_time_ms=int(updated) if updated not in (None, "") else None,
    )


class BinanceDemoPrivateReadOnlyClient:
    def __init__(
        self,
        credentials: BinanceDemoCredentials,
        base_url: str = BINANCE_DEMO_REST_URL,
        *,
        recv_window_ms: int = 5000,
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
        time_source_ms: Callable[[], int] | None = None,
    ) -> None:
        credentials.validate()
        if not 1 <= recv_window_ms <= 60000:
            raise ValueError("recv_window_ms must be between 1 and 60000")
        self.credentials = credentials
        self.base_url = assert_binance_demo_url(base_url).rstrip("/")
        self.recv_window_ms = recv_window_ms
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._time_source_ms = time_source_ms or (lambda: time.time_ns() // 1_000_000)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BinanceDemoPrivateReadOnlyClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _signed_get(self, path: str, params: dict[str, object] | None = None) -> Any:
        if path not in _ALLOWED_READ_PATHS:
            raise BinancePrivateApiError(f"private read path is not allowlisted: {path}")
        query_params = dict(params or {})
        query_params["recvWindow"] = self.recv_window_ms
        query_params["timestamp"] = self._time_source_ms()
        query = encode_query(query_params)
        signature = sign_query(self.credentials.api_secret, query)
        url = f"{self.base_url}{path}?{query}&signature={signature}"
        assert_binance_demo_url(url)
        response = self._client.get(url, headers={"X-MBX-APIKEY": self.credentials.api_key})
        try:
            payload = response.json()
        except ValueError as exc:
            raise BinancePrivateApiError("Binance private response is not JSON") from exc
        if response.is_error:
            if isinstance(payload, dict):
                raise BinancePrivateApiError(
                    f"Binance private API error code={payload.get('code')} msg={payload.get('msg')}"
                )
            response.raise_for_status()
        return payload

    def get_wallet_balance(self) -> WalletSnapshot:
        payload = self._signed_get("/fapi/v2/account")
        if not isinstance(payload, dict):
            raise BinancePrivateApiError("account response must be a JSON object")
        coins: list[WalletCoin] = []
        for asset in payload.get("assets", []):
            if not isinstance(asset, dict):
                continue
            wallet_balance = decimal_required(asset.get("walletBalance"), "asset.walletBalance")
            unrealised = decimal_optional(asset.get("unrealizedProfit")) or Decimal(0)
            coins.append(
                WalletCoin(
                    coin=str(asset.get("asset", "")),
                    equity=wallet_balance + unrealised,
                    wallet_balance=wallet_balance,
                    usd_value=None,
                    unrealised_pnl=unrealised,
                    cum_realised_pnl=None,
                )
            )
        return WalletSnapshot(
            account_type="FUTURES_DEMO",
            total_equity=decimal_optional(payload.get("totalMarginBalance")),
            total_wallet_balance=decimal_optional(payload.get("totalWalletBalance")),
            total_margin_balance=decimal_optional(payload.get("totalMarginBalance")),
            total_available_balance=decimal_optional(payload.get("availableBalance")),
            total_perp_upl=decimal_optional(payload.get("totalUnrealizedProfit")),
            coins=tuple(coins),
        )

    def get_positions(self) -> tuple[PositionSnapshot, ...]:
        payload = self._signed_get("/fapi/v2/positionRisk")
        if not isinstance(payload, list):
            raise BinancePrivateApiError("position response must be a JSON array")
        positions: list[PositionSnapshot] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            amount = decimal_required(item.get("positionAmt"), "position.positionAmt")
            side = "Buy" if amount > 0 else "Sell" if amount < 0 else ""
            update_time = item.get("updateTime")
            positions.append(
                PositionSnapshot(
                    symbol=str(item.get("symbol", "")),
                    side=side,
                    size=abs(amount),
                    avg_price=decimal_optional(item.get("entryPrice")),
                    position_value=(
                        abs(decimal_required(item.get("notional"), "position.notional"))
                        if item.get("notional") not in (None, "")
                        else None
                    ),
                    leverage=decimal_optional(item.get("leverage")),
                    mark_price=decimal_optional(item.get("markPrice")),
                    liq_price=decimal_optional(item.get("liquidationPrice")),
                    unrealised_pnl=decimal_optional(item.get("unRealizedProfit")),
                    cum_realised_pnl=None,
                    position_im=decimal_optional(item.get("initialMargin")),
                    position_mm=decimal_optional(item.get("maintMargin")),
                    take_profit=None,
                    stop_loss=None,
                    trailing_stop=None,
                    updated_time_ms=(
                        int(update_time) if update_time not in (None, "") else None
                    ),
                )
            )
        return tuple(positions)

    def get_open_orders(self) -> tuple[OrderSnapshot, ...]:
        payload = self._signed_get("/fapi/v1/openOrders")
        if not isinstance(payload, list):
            raise BinancePrivateApiError("open-order response must be a JSON array")
        return tuple(_parse_order(item) for item in payload if isinstance(item, dict))

    def get_order_by_client_id(self, symbol: str, client_order_id: str) -> OrderSnapshot:
        payload = self._signed_get(
            "/fapi/v1/order",
            {"symbol": symbol.upper(), "origClientOrderId": client_order_id},
        )
        if not isinstance(payload, dict):
            raise BinancePrivateApiError("order response must be a JSON object")
        return _parse_order(payload)

    def get_algo_order_by_client_id(self, client_algo_id: str) -> AlgoOrderSnapshot:
        payload = self._signed_get(
            "/fapi/v1/algoOrder",
            {"clientAlgoId": client_algo_id},
        )
        if not isinstance(payload, dict):
            raise BinancePrivateApiError("algo order response must be a JSON object")
        updated = payload.get("updateTime") or payload.get("createTime")
        quantity = payload.get("quantity") or payload.get("origQty")
        trigger_price = payload.get("triggerPrice") or payload.get("stopPrice")
        return AlgoOrderSnapshot(
            algo_id=str(payload.get("algoId") or ""),
            client_algo_id=str(payload.get("clientAlgoId") or ""),
            symbol=str(payload.get("symbol") or ""),
            side=str(payload.get("side") or ""),
            order_type=str(payload.get("type") or ""),
            status=str(payload.get("algoStatus") or payload.get("status") or ""),
            trigger_price=decimal_optional(trigger_price),
            quantity=decimal_optional(quantity),
            reduce_only=bool(payload.get("reduceOnly", False)),
            updated_time_ms=int(updated) if updated not in (None, "") else None,
        )
