from __future__ import annotations

import time
from collections.abc import Callable

from crypto_scanner.binance.private_rest import (
    AlgoOrderSnapshot,
    BinanceDemoPrivateReadOnlyClient,
    BinancePrivateApiError,
)

_TRANSIENT_ALGO_NOT_VISIBLE = "code=-2013"
_ACTIVE_ALGO_STATUSES = frozenset({"NEW", "PENDING", "WORKING"})


def _find_open_algo_by_client_id(
    reader: BinanceDemoPrivateReadOnlyClient,
    client_algo_id: str,
) -> AlgoOrderSnapshot | None:
    """Cross-check the authoritative open-algo collection after a transient -2013.

    Binance can acknowledge a conditional order before the single-order lookup endpoint
    indexes its clientAlgoId. The open-algo collection can become consistent first. A
    unique active match is therefore sufficient reconciliation evidence; absence is not
    treated as evidence that submission failed.
    """
    try:
        orders = reader.get_open_algo_orders()
    except (AttributeError, BinancePrivateApiError):
        return None

    matches = tuple(
        order
        for order in orders
        if order.client_algo_id == client_algo_id
        and order.status.upper() in _ACTIVE_ALGO_STATUSES
    )
    if len(matches) > 1:
        raise RuntimeError(
            f"duplicate active algo orders share clientAlgoId={client_algo_id}"
        )
    return matches[0] if matches else None


def get_algo_order_eventually(
    reader: BinanceDemoPrivateReadOnlyClient,
    client_algo_id: str,
    *,
    attempts: int = 12,
    delay_seconds: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
) -> AlgoOrderSnapshot:
    """Reconcile a just-created algo order across bounded Binance propagation delay.

    Only the specific `-2013 Order does not exist` read-after-write race is retried.
    After each transient miss the open-algo collection is cross-checked by the same
    deterministic clientAlgoId. This prevents a successful POST from being mistaken
    for a failed submission and, critically, prevents a later management tick from
    installing another replacement pair while the first pair is already active.

    Every other private API failure propagates immediately. If neither endpoint can
    prove the order active within the bounded window, the final `-2013` remains a hard
    fail-closed result; callers must never blind-resubmit on that ambiguity.
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

            open_match = _find_open_algo_by_client_id(reader, client_algo_id)
            if open_match is not None:
                return open_match

            if attempt + 1 < attempts:
                sleep(delay_seconds)

    # One final collection read covers a boundary where the order becomes visible just
    # after the last single-order lookup. Absence still means unknown, not "safe to retry".
    open_match = _find_open_algo_by_client_id(reader, client_algo_id)
    if open_match is not None:
        return open_match

    assert last_error is not None
    raise last_error
