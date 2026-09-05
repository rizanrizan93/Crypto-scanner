from __future__ import annotations

import json
from typing import Any

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient


def build_readonly_report() -> dict[str, Any]:
    credentials = BinanceDemoCredentials.from_environment()
    with BinanceDemoPrivateReadOnlyClient(credentials) as client:
        wallet = client.get_wallet_balance()
        positions = client.get_positions()
        orders = client.get_open_orders()

    return {
        "venue": "BINANCE",
        "environment": "DEMO",
        "mode": "PRIVATE_READ_ONLY",
        "wallet": {
            "account_type": wallet.account_type,
            "total_equity": str(wallet.total_equity) if wallet.total_equity is not None else None,
            "total_wallet_balance": (
                str(wallet.total_wallet_balance)
                if wallet.total_wallet_balance is not None
                else None
            ),
            "total_available_balance": (
                str(wallet.total_available_balance)
                if wallet.total_available_balance is not None
                else None
            ),
        },
        "positions": [
            {
                "symbol": position.symbol,
                "side": position.side,
                "size": str(position.size),
                "avg_price": str(position.avg_price) if position.avg_price is not None else None,
                "mark_price": str(position.mark_price) if position.mark_price is not None else None,
            }
            for position in positions
            if position.is_open
        ],
        "open_orders": [
            {
                "order_id": order.order_id,
                "order_link_id": order.order_link_id,
                "symbol": order.symbol,
                "side": order.side,
                "status": order.order_status,
                "qty": str(order.qty),
                "price": str(order.price) if order.price is not None else None,
            }
            for order in orders
        ],
    }


def main() -> None:
    print(json.dumps(build_readonly_report(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
