from __future__ import annotations

import time
from collections.abc import Callable

from crypto_scanner.binance.private_rest import (
    AlgoOrderSnapshot,
    BinanceDemoPrivateReadOnlyClient,
    BinancePrivateApiError,
)

_TRANSIENT_ALGO_NOT_VISIBLE = "code=-2013"


def get_algo_order_eventually(
    reader: BinanceDemoPrivateReadOnlyClient,
    client_algo_id: str,
    *,
    attempts: int = 6,
    delay_seconds: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
) -> AlgoOrderSnapshot:
    """Reconcile a just-created algo order across bounded Binance propagation delay.

    Only the specific `-2013 Order does not exist` read-after-write race is retried.
    Every other private API failure propagates immediately, and the final `-2013`
    remains a hard failure after the bounded window.
    """
    if not client_algo_id.strip():
        raise ValueError("client_algo_id is required")
    if not 1 <= attempts <= 20:
        raise ValueError("algo reconciliation attempts must be between 1 and 20")
    if delay_seconds < 0 or delay_seconds > 2:
        raise ValueError("algo reconciliation delay must be between 0 and 2 seconds")

    last_error: BinancePrivateApiError | None = None
    for attempt in range(attempts):
        try:
            return reader.get_algo_order_by_client_id(client_algo_id)
        except BinancePrivateApiError as exc:
            if _TRANSIENT_ALGO_NOT_VISIBLE not in str(exc):
                raise
            last_error = exc
            if attempt + 1 < attempts:
                sleep(delay_seconds)

    assert last_error is not None
    raise last_error
