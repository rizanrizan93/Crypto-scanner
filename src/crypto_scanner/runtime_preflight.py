from __future__ import annotations

import json
import platform
import socket
from typing import Any

from crypto_scanner.bybit.auth import BybitTestnetCredentials
from crypto_scanner.bybit.private_rest import BybitPrivateReadOnlyClient
from crypto_scanner.bybit.public_rest import BybitPublicRestClient
from crypto_scanner.config import load_runtime_config
from crypto_scanner.execution_plan import TestnetExecutionArm


class RuntimePreflightError(RuntimeError):
    """Raised when the operational Testnet host is not safe to arm."""


def run_runtime_preflight() -> dict[str, Any]:
    config = load_runtime_config()
    config.validate()

    arm = TestnetExecutionArm.from_environment()
    if arm.enabled:
        raise RuntimePreflightError(
            "preflight requires CRYPTO_SCANNER_TESTNET_EXECUTION to remain DISABLED"
        )

    credentials = BybitTestnetCredentials.from_environment()
    report: dict[str, Any] = {
        "venue": "BYBIT",
        "environment": "TESTNET",
        "live_trading_locked": config.safety.live_trading_locked,
        "testnet_execution_armed": False,
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "universe": list(config.universe),
    }

    public_symbols: dict[str, object] = {}
    with BybitPublicRestClient(base_url=config.bybit_rest_url) as public:
        for symbol in config.universe:
            instrument = public.get_instrument(symbol)
            ticker = public.get_ticker(symbol)
            if instrument.status != "Trading" or instrument.settle_coin != "USDT":
                raise RuntimePreflightError(f"{symbol} is not an active USDT contract")
            if ticker.ask_price <= ticker.bid_price:
                raise RuntimePreflightError(f"{symbol} returned an invalid bid/ask quote")
            public_symbols[symbol] = {
                "status": instrument.status,
                "tick_size": str(instrument.tick_size),
                "qty_step": str(instrument.qty_step),
                "min_order_qty": str(instrument.min_order_qty),
                "min_notional_value": (
                    str(instrument.min_notional_value)
                    if instrument.min_notional_value is not None
                    else None
                ),
                "mark_price": str(ticker.mark_price),
                "spread_bps": str(ticker.spread_bps),
            }
    report["public_symbols"] = public_symbols

    with BybitPrivateReadOnlyClient(
        credentials,
        base_url=config.bybit_rest_url,
    ) as private:
        wallet = private.get_wallet_balance()
        positions = private.get_positions()
        orders = private.get_open_orders()

    if wallet.account_type != "UNIFIED":
        raise RuntimePreflightError("Bybit account is not UNIFIED")
    if wallet.total_equity is None or wallet.total_equity <= 0:
        raise RuntimePreflightError("Bybit Testnet equity is missing or non-positive")

    report["private_account"] = {
        "account_type": wallet.account_type,
        "total_equity": str(wallet.total_equity),
        "total_available_balance": (
            str(wallet.total_available_balance)
            if wallet.total_available_balance is not None
            else None
        ),
        "open_positions": [
            {
                "symbol": position.symbol,
                "side": position.side,
                "size": str(position.size),
            }
            for position in positions
        ],
        "open_orders": [
            {
                "symbol": order.symbol,
                "side": order.side,
                "status": order.order_status,
                "order_link_id": order.order_link_id,
            }
            for order in orders
        ],
    }
    report["preflight_status"] = "PASS_DISARMED"
    return report


def main() -> None:
    print(json.dumps(run_runtime_preflight(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
