from __future__ import annotations

import time
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
    """Raised when a Binance Futures Demo read-only request fails."""


_ALLOWED_READ_PATHS = frozenset(
    {
        "/fapi/v2/account",
        "/fapi/v2/positionRisk",
        "/fapi/v1/openOrders",
    }
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
    ) -> None:
        credentials.validate()
        if not 1 <= recv_window_ms <= 60000:
            raise ValueError("recv_window_ms must be between 1 and 60000")
        self.credentials = credentials
        self.base_url = assert_binance_demo_url(base_url).rstrip("/")
        self.recv_window_ms = recv_window_ms
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)

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
        timestamp_ms = time.time_ns() // 1_000_000
        query_params = dict(params or {})
        query_params["recvWindow"] = self.recv_window_ms
        query_params["timestamp"] = timestamp_ms
        query = encode_query(query_params)
        signature = sign_query(self.credentials.api_secret, query)
        url = f"{self.base_url}{path}?{query}&signature={signature}"
        assert_binance_demo_url(url)
        response = self._client.get(url, headers={"X-MBX-APIKEY": self.credentials.api_key})
        payload = response.json()
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
            if amount > 0:
                side = "Buy"
            elif amount < 0:
                side = "Sell"
            else:
                side = ""
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
        orders: list[OrderSnapshot] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            executed_qty = decimal_optional(item.get("executedQty"))
            orig_qty = decimal_required(item.get("origQty"), "order.origQty")
            created = item.get("time")
            updated = item.get("updateTime")
            orders.append(
                OrderSnapshot(
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
            )
        return tuple(orders)
