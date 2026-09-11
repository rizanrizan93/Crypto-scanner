from __future__ import annotations

import json
import os
import time
from contextlib import suppress
from decimal import Decimal

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_rest import (
    BinanceDemoPrivateReadOnlyClient,
    BinancePrivateApiError,
)
from crypto_scanner.binance.private_write import (
    BinanceTestnetOrderClient,
    UnknownSubmissionOutcome,
    deterministic_management_id,
)
from crypto_scanner.execution_plan import TestnetExecutionArm


SYMBOL = "DOGEUSDT"
CONFIRMATION = "CLOSE_DOGEUSDT_DEMO"


def _reconcile_exit(
    reader: BinanceDemoPrivateReadOnlyClient,
    *,
    client_order_id: str,
    exit_side: str,
    expected_qty: Decimal,
) -> tuple[str, Decimal]:
    last_status = "UNKNOWN"
    for attempt in range(12):
        try:
            order = reader.get_order_by_client_id(SYMBOL, client_order_id)
        except BinancePrivateApiError as exc:
            if "code=-2013" not in str(exc) or attempt == 11:
                raise
            time.sleep(0.25)
            continue
        last_status = order.order_status
        executed = order.cum_exec_qty or Decimal(0)
        if order.order_status == "FILLED":
            if (
                order.order_link_id != client_order_id
                or order.symbol != SYMBOL
                or order.side != ("Buy" if exit_side == "BUY" else "Sell")
                or not order.reduce_only
                or executed < expected_qty
            ):
                raise RuntimeError("emergency exit identity or quantity mismatch")
            return order.order_id, executed
        if order.order_status in {"CANCELED", "EXPIRED", "REJECTED"}:
            break
        time.sleep(0.25)
    raise RuntimeError(f"emergency reduce-only exit did not fill: {last_status}")


def main() -> None:
    if os.getenv("CRYPTO_SCANNER_EMERGENCY_SYMBOL") != SYMBOL:
        raise RuntimeError("emergency close is hard-pinned to DOGEUSDT")
    if os.getenv("CRYPTO_SCANNER_EMERGENCY_CONFIRM") != CONFIRMATION:
        raise RuntimeError("exact DOGEUSDT Demo close confirmation is required")

    arm = TestnetExecutionArm.from_environment()
    arm.require_enabled()
    credentials = BinanceDemoCredentials.from_environment()

    with (
        BinanceDemoPrivateReadOnlyClient(credentials) as reader,
        BinanceTestnetOrderClient(credentials, arm) as writer,
    ):
        positions = tuple(
            position
            for position in reader.get_positions()
            if position.symbol == SYMBOL and position.is_open
        )
        if not positions:
            print(
                json.dumps(
                    {
                        "environment": "DEMO",
                        "live_trading_locked": True,
                        "remaining_qty": "0",
                        "status": "ALREADY_FLAT",
                        "symbol": SYMBOL,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return
        if len(positions) != 1:
            raise RuntimeError("expected exactly one DOGEUSDT One-way position")

        position = positions[0]
        exit_side = "SELL" if position.side == "Buy" else "BUY"
        client_order_id = deterministic_management_id(
            SYMBOL,
            "operator-authorized-20260911-doge-demo",
            "panic",
        )
        with suppress(UnknownSubmissionOutcome):
            writer.submit_reduce_only_market_exit(
                symbol=SYMBOL,
                exit_side=exit_side,
                qty=position.size,
                client_order_id=client_order_id,
            )
        venue_order_id, closed_qty = _reconcile_exit(
            reader,
            client_order_id=client_order_id,
            exit_side=exit_side,
            expected_qty=position.size,
        )

        remaining = ()
        for attempt in range(12):
            remaining = tuple(
                item
                for item in reader.get_positions()
                if item.symbol == SYMBOL and item.is_open
            )
            if not remaining:
                break
            if attempt < 11:
                time.sleep(0.25)
        algo_orders = tuple(
            order
            for order in reader.get_open_algo_orders(SYMBOL)
            if order.symbol == SYMBOL
        )
        regular_orders = tuple(
            order
            for order in reader.get_open_orders()
            if order.symbol == SYMBOL
        )
        if remaining:
            raise RuntimeError("DOGEUSDT emergency close did not prove flat")

        print(
            json.dumps(
                {
                    "client_order_id": client_order_id,
                    "closed_qty": str(closed_qty),
                    "environment": "DEMO",
                    "initial_leverage": str(position.leverage),
                    "initial_side": position.side,
                    "live_trading_locked": True,
                    "open_algo_orders": len(algo_orders),
                    "open_regular_orders": len(regular_orders),
                    "remaining_qty": "0",
                    "status": "CLOSED_AND_VERIFIED_FLAT",
                    "symbol": SYMBOL,
                    "venue_order_id": venue_order_id,
                },
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
