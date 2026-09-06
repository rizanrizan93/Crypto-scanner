from __future__ import annotations

import asyncio
import json

import websockets

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.private_ws import BinanceDemoListenKeyClient
from crypto_scanner.execution_plan import TestnetExecutionArm
from crypto_scanner.safety import BINANCE_DEMO_WS_STREAM_URL, assert_binance_demo_ws_url


class PrivateWsProbeError(RuntimeError):
    """Raised when the Binance Demo private WebSocket cannot be proven safely."""


async def probe_private_websocket() -> dict[str, object]:
    arm = TestnetExecutionArm.from_environment()
    if arm.enabled:
        raise PrivateWsProbeError("private websocket probe requires Testnet execution DISABLED")

    credentials = BinanceDemoCredentials.from_environment()
    with BinanceDemoListenKeyClient(credentials) as listen:
        listen_key = listen.start()
        url = assert_binance_demo_ws_url(
            f"{BINANCE_DEMO_WS_STREAM_URL}/ws/{listen_key}"
        )
        try:
            async with websockets.connect(
                url,
                open_timeout=8,
                close_timeout=3,
                ping_interval=None,
                max_size=2**20,
            ) as socket:
                pong = await socket.ping()
                await asyncio.wait_for(pong, timeout=5)
        finally:
            listen.stop()

    return {
        "status": "PASS_PRIVATE_WS_HANDSHAKE",
        "venue": "BINANCE",
        "environment": "DEMO",
        "execution_armed": False,
        "live_trading_locked": True,
        "listen_key_created": True,
        "websocket_handshake": True,
        "ping_pong": True,
    }


def main() -> None:
    report = asyncio.run(probe_private_websocket())
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
