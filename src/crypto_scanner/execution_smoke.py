from __future__ import annotations

import json
import os
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.models import InstrumentInfo, TickerSnapshot, WalletSnapshot
from crypto_scanner.binance.private_rest import BinanceDemoPrivateReadOnlyClient
from crypto_scanner.binance.private_write import (
    BinanceTestnetOrderClient,
    build_protection_plan,
)
from crypto_scanner.binance.public_rest import BinanceDemoPublicRestClient
from crypto_scanner.execution_plan import (
    EntryOrderPlan,
    TestnetExecutionArm,
    deterministic_order_link_id,
)

SMOKE_SYMBOL = "XRPUSDT"
MAX_SMOKE_NOTIONAL = Decimal("10")


class ExecutionSmokeError(RuntimeError):
    """Raised when the one-time Binance Futures execution smoke cannot proceed safely."""


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ExecutionSmokeError("step must be positive")
    units = (value / step).to_integral_value(rounding=ROUND_CEILING)
    return units * step


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ExecutionSmokeError("step must be positive")
    units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return units * step


def require_smoke_confirmation() -> None:
    if os.getenv("CRYPTO_SCANNER_EXECUTION_SMOKE", "") != "CONFIRMED":
        raise ExecutionSmokeError(
            "one-time execution smoke is not confirmed; "
            "CRYPTO_SCANNER_EXECUTION_SMOKE=CONFIRMED required"
        )


def build_smoke_plan(
    *,
    instrument: InstrumentInfo,
    ticker: TickerSnapshot,
    wallet: WalletSnapshot,
    smoke_id: str,
) -> EntryOrderPlan:
    if instrument.symbol != SMOKE_SYMBOL or ticker.symbol != SMOKE_SYMBOL:
        raise ExecutionSmokeError("execution smoke is hard-pinned to XRPUSDT")
    if instrument.status != "Trading" or instrument.settle_coin != "USDT":
        raise ExecutionSmokeError("XRPUSDT is not an active USDT futures contract")
    if wallet.total_equity is None or wallet.total_equity <= 0:
        raise ExecutionSmokeError("authoritative Demo equity is missing or invalid")
    if not smoke_id.strip():
        raise ExecutionSmokeError("CRYPTO_SCANNER_SMOKE_ID is required")
    if ticker.ask_price <= 0 or ticker.mark_price <= 0:
        raise ExecutionSmokeError("current XRPUSDT quote is invalid")

    min_notional = instrument.min_notional_value or Decimal(0)
    qty_for_notional = (
        _ceil_to_step(min_notional / ticker.ask_price, instrument.qty_step)
        if min_notional > 0
        else instrument.min_order_qty
    )
    qty = max(instrument.min_order_qty, qty_for_notional)
    qty = _ceil_to_step(qty, instrument.qty_step)
    if instrument.max_market_order_qty is not None and qty > instrument.max_market_order_qty:
        raise ExecutionSmokeError("minimum smoke quantity exceeds exchange market-order maximum")

    notional = qty * ticker.ask_price
    if notional < min_notional:
        raise ExecutionSmokeError("computed smoke order is below minimum notional")
    if notional > MAX_SMOKE_NOTIONAL:
        raise ExecutionSmokeError(
            f"minimum valid smoke notional {notional} exceeds {MAX_SMOKE_NOTIONAL} USDT cap"
        )

    stop_loss = _floor_to_step(ticker.mark_price * Decimal("0.99"), instrument.tick_size)
    take_profit_1 = _ceil_to_step(ticker.mark_price * Decimal("1.01"), instrument.tick_size)
    take_profit_2 = _ceil_to_step(ticker.mark_price * Decimal("1.02"), instrument.tick_size)
    if not Decimal(0) < stop_loss < ticker.bid_price:
        raise ExecutionSmokeError("computed smoke stop is not safely below the current bid")
    if not ticker.ask_price < take_profit_1 < take_profit_2:
        raise ExecutionSmokeError("computed smoke take-profit geometry is invalid")

    risk_amount = qty * (ticker.ask_price - stop_loss)
    risk_fraction = risk_amount / wallet.total_equity
    if risk_fraction <= 0 or risk_fraction > Decimal("0.001"):
        raise ExecutionSmokeError("execution smoke risk exceeds 0.10% equity cap")

    signal_id = f"execution-smoke:{smoke_id}"
    return EntryOrderPlan(
        signal_id=signal_id,
        order_link_id=deterministic_order_link_id(SMOKE_SYMBOL, signal_id),
        symbol=SMOKE_SYMBOL,
        side="Buy",
        qty=qty,
        entry_price=ticker.ask_price,
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        risk_fraction=risk_fraction,
        risk_amount=risk_amount,
        notional=notional,
        leverage_equivalent=notional / wallet.total_equity,
    )


def _wait_for_entry(
    private: BinanceDemoPrivateReadOnlyClient,
    plan: EntryOrderPlan,
    *,
    attempts: int = 12,
) -> tuple[str, Decimal]:
    last_status = ""
    last_filled = Decimal(0)
    for _ in range(attempts):
        order = private.get_order_by_client_id(plan.symbol, plan.order_link_id)
        last_status = order.order_status
        last_filled = order.cum_exec_qty or Decimal(0)
        if last_status == "FILLED":
            return last_status, last_filled
        if last_status in {"CANCELED", "EXPIRED", "REJECTED"}:
            break
        time.sleep(0.5)
    if last_filled > 0:
        return last_status, last_filled
    raise ExecutionSmokeError(
        f"entry did not produce a confirmed fill; status={last_status or 'UNKNOWN'}"
    )


def main() -> None:
    require_smoke_confirmation()
    arm = TestnetExecutionArm.from_environment()
    arm.require_enabled()
    smoke_id = os.getenv("CRYPTO_SCANNER_SMOKE_ID", "").strip()
    credentials = BinanceDemoCredentials.from_environment()

    with (
        BinanceDemoPublicRestClient() as public,
        BinanceDemoPrivateReadOnlyClient(credentials) as private,
        BinanceTestnetOrderClient(credentials, arm) as writer,
    ):
        wallet = private.get_wallet_balance()
        positions = tuple(position for position in private.get_positions() if position.is_open)
        open_orders = private.get_open_orders()
        if positions:
            raise ExecutionSmokeError("execution smoke requires zero pre-existing positions")
        if open_orders:
            raise ExecutionSmokeError("execution smoke requires zero pre-existing open orders")
        if private.get_position_mode_is_hedged():
            raise ExecutionSmokeError(
                "execution smoke requires Binance One-way Mode, not Hedge Mode"
            )

        confirmed_leverage = writer.set_leverage(SMOKE_SYMBOL, 1)
        if confirmed_leverage != 1:
            raise ExecutionSmokeError("failed to confirm 1x leverage before entry")

        instrument = public.get_instrument(SMOKE_SYMBOL)
        ticker = public.get_ticker(SMOKE_SYMBOL)
        plan = build_smoke_plan(
            instrument=instrument,
            ticker=ticker,
            wallet=wallet,
            smoke_id=smoke_id,
        )

        entry_ack = writer.submit_entry(plan)
        entry_status, filled_qty = _wait_for_entry(private, plan)
        if filled_qty <= 0:
            raise ExecutionSmokeError("confirmed entry has zero filled quantity")

        protection = build_protection_plan(plan, filled_qty)
        stop_ack = writer.submit_stop_loss(protection)
        stop_state = private.get_algo_order_by_client_id(stop_ack.client_algo_id)
        tp_ack = writer.submit_take_profit(protection)
        tp_state = private.get_algo_order_by_client_id(tp_ack.client_algo_id)

        current_positions = tuple(
            position
            for position in private.get_positions()
            if position.is_open and position.symbol == plan.symbol
        )
        if len(current_positions) != 1:
            raise ExecutionSmokeError("expected exactly one protected XRPUSDT Demo position")

        print(
            json.dumps(
                {
                    "status": "PASS_PROTECTED_TESTNET_EXECUTION",
                    "environment": "DEMO",
                    "venue": "BINANCE",
                    "live_trading_locked": True,
                    "symbol": plan.symbol,
                    "side": plan.side,
                    "exchange_leverage": confirmed_leverage,
                    "planned_qty": str(plan.qty),
                    "filled_qty": str(filled_qty),
                    "planned_notional": str(plan.notional),
                    "risk_amount": str(plan.risk_amount),
                    "risk_fraction": str(plan.risk_fraction),
                    "entry_order_id": entry_ack.order_id,
                    "entry_client_order_id": entry_ack.client_order_id,
                    "entry_status": entry_status,
                    "stop_algo_id": stop_ack.algo_id,
                    "stop_client_algo_id": stop_ack.client_algo_id,
                    "stop_status": stop_state.status,
                    "stop_trigger": str(protection.stop_loss),
                    "tp2_algo_id": tp_ack.algo_id,
                    "tp2_client_algo_id": tp_ack.client_algo_id,
                    "tp2_status": tp_state.status,
                    "tp2_trigger": str(protection.take_profit),
                    "open_position_size": str(current_positions[0].size),
                },
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
