from __future__ import annotations

import json
import os

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient
from crypto_scanner.binance.private_write import BinanceTestnetOrderClient
from crypto_scanner.emergency_exit import flatten_scanner_position
from crypto_scanner.execution_plan import TestnetExecutionArm


SYMBOL = "DOGEUSDT"
CONFIRMATION = "CLOSE_DOGEUSDT_DEMO"


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
        result = flatten_scanner_position(
            reader,
            writer,
            symbol=SYMBOL,
            management_seed="operator-authorized-20260911-doge-demo",
        )

        remaining = tuple(
            item
            for item in reader.get_positions()
            if item.symbol == SYMBOL and item.is_open
        )
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
                    "client_order_id": result.client_order_id,
                    "closed_qty": str(result.exited_qty),
                    "environment": "DEMO",
                    "initial_leverage": str(position.leverage),
                    "initial_side": position.side,
                    "live_trading_locked": True,
                    "open_algo_orders": len(algo_orders),
                    "open_regular_orders": len(regular_orders),
                    "remaining_qty": "0",
                    "status": "CLOSED_AND_VERIFIED_FLAT",
                    "symbol": SYMBOL,
                    "venue_order_id": result.venue_order_id,
                },
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
