from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from crypto_scanner.bybit.auth import (
    BybitTestnetCredentials,
    encode_query,
    sign_get_request,
)
from crypto_scanner.bybit.private_models import (
    OrderSnapshot,
    PositionSnapshot,
    WalletSnapshot,
    parse_order_snapshot,
    parse_position_snapshot,
    parse_wallet_snapshot,
)
from crypto_scanner.bybit.public_rest import BybitAccessForbiddenError
from crypto_scanner.safety import BYBIT_TESTNET_REST_URL, assert_testnet_url


class BybitPrivateApiError(RuntimeError):
    """Raised when a Bybit private read request fails."""


class BybitReadOnlyViolation(BybitPrivateApiError):
    """Raised when code attempts to access a path outside the read-only contract."""


_READ_ONLY_PATHS = frozenset(
    {
        "/v5/account/wallet-balance",
        "/v5/position/list",
        "/v5/order/realtime",
        "/v5/order/history",
    }
)


class BybitPrivateReadOnlyClient:
    def __init__(
        self,
        credentials: BybitTestnetCredentials,
        *,
        base_url: str = BYBIT_TESTNET_REST_URL,
        recv_window_ms: int = 5000,
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
        time_source_ms: Callable[[], int] | None = None,
    ) -> None:
        credentials.validate()
        if not 1 <= recv_window_ms <= 5000:
            raise ValueError("recv_window_ms must be between 1 and 5000")
        self._credentials = credentials
        self.base_url = assert_testnet_url(base_url).rstrip("/")
        self.recv_window_ms = recv_window_ms
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._time_source_ms = time_source_ms or (lambda: time.time_ns() // 1_000_000)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BybitPrivateReadOnlyClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _signed_get(self, path: str, params: dict[str, object]) -> dict[str, Any]:
        if path not in _READ_ONLY_PATHS:
            raise BybitReadOnlyViolation(f"private read path is not allowed: {path}")

        query_string = encode_query(params)
        timestamp_ms = self._time_source_ms()
        signature = sign_get_request(
            api_key=self._credentials.api_key,
            api_secret=self._credentials.api_secret,
            timestamp_ms=timestamp_ms,
            recv_window_ms=self.recv_window_ms,
            query_string=query_string,
        )
        url = f"{self.base_url}{path}"
        if query_string:
            url = f"{url}?{query_string}"
        assert_testnet_url(url)
        headers = {
            "X-BAPI-API-KEY": self._credentials.api_key,
            "X-BAPI-TIMESTAMP": str(timestamp_ms),
            "X-BAPI-SIGN": signature,
            "X-BAPI-RECV-WINDOW": str(self.recv_window_ms),
            "Content-Type": "application/json",
        }
        response = self._client.get(url, headers=headers)
        if response.status_code == 403:
            raise BybitAccessForbiddenError(
                "Bybit Testnet private endpoint returned HTTP 403. Verify the runtime source IP, "
                "request controls, and account eligibility before retrying."
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise BybitPrivateApiError("Bybit private response must be a JSON object")
        if payload.get("retCode") != 0:
            raise BybitPrivateApiError(
                f"Bybit private API error retCode={payload.get('retCode')} "
                f"retMsg={payload.get('retMsg')}"
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise BybitPrivateApiError("Bybit private response is missing result object")
        return result

    def get_wallet_balance(self, *, coin: str = "USDT") -> WalletSnapshot:
        result = self._signed_get(
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED", "coin": coin.upper()},
        )
        items = result.get("list") or []
        if len(items) != 1 or not isinstance(items[0], dict):
            raise BybitPrivateApiError(
                f"expected one UNIFIED wallet record, received {len(items)}"
            )
        wallet = parse_wallet_snapshot(items[0])
        if wallet.account_type != "UNIFIED":
            raise BybitPrivateApiError(
                f"unexpected account type from Bybit: {wallet.account_type or '<empty>'}"
            )
        return wallet

    def get_positions(
        self,
        *,
        settle_coin: str = "USDT",
        limit: int = 200,
    ) -> tuple[PositionSnapshot, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("position limit must be between 1 and 200")
        rows = self._paginate(
            "/v5/position/list",
            {
                "category": "linear",
                "settleCoin": settle_coin.upper(),
                "limit": limit,
            },
            max_pages=10,
        )
        positions = tuple(
            parse_position_snapshot(item) for item in rows if isinstance(item, dict)
        )
        return tuple(position for position in positions if position.is_open)

    def get_open_orders(
        self,
        *,
        settle_coin: str = "USDT",
        limit: int = 50,
    ) -> tuple[OrderSnapshot, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("open-order limit must be between 1 and 50")
        rows = self._paginate(
            "/v5/order/realtime",
            {
                "category": "linear",
                "settleCoin": settle_coin.upper(),
                "openOnly": 0,
                "limit": limit,
            },
            max_pages=10,
        )
        return tuple(parse_order_snapshot(item) for item in rows if isinstance(item, dict))

    def get_order_by_link_id(
        self,
        *,
        symbol: str,
        order_link_id: str,
    ) -> OrderSnapshot | None:
        if not order_link_id.strip():
            raise ValueError("order_link_id cannot be empty")
        params = {
            "category": "linear",
            "symbol": symbol.upper(),
            "orderLinkId": order_link_id,
            "limit": 1,
        }
        realtime = self._signed_get("/v5/order/realtime", params)
        rows = realtime.get("list") or []
        if rows and isinstance(rows[0], dict):
            return parse_order_snapshot(rows[0])

        history = self._signed_get("/v5/order/history", params)
        rows = history.get("list") or []
        if rows and isinstance(rows[0], dict):
            return parse_order_snapshot(rows[0])
        return None

    def _paginate(
        self,
        path: str,
        initial_params: dict[str, object],
        *,
        max_pages: int,
    ) -> tuple[dict[str, object], ...]:
        params = dict(initial_params)
        rows: list[dict[str, object]] = []
        seen_cursors: set[str] = set()

        for _ in range(max_pages):
            result = self._signed_get(path, params)
            page_rows = result.get("list") or []
            for item in page_rows:
                if isinstance(item, dict):
                    rows.append(item)

            cursor = str(result.get("nextPageCursor") or "")
            if not cursor:
                return tuple(rows)
            if cursor in seen_cursors:
                raise BybitPrivateApiError("Bybit pagination cursor repeated; refusing stale loop")
            seen_cursors.add(cursor)
            params["cursor"] = cursor

        raise BybitPrivateApiError(
            f"pagination exceeded safety bound of {max_pages} pages for {path}"
        )
