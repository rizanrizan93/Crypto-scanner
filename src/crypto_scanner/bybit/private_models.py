from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from crypto_scanner.bybit.models import decimal_optional, decimal_required


@dataclass(frozen=True, slots=True)
class WalletCoin:
    coin: str
    equity: Decimal
    wallet_balance: Decimal
    usd_value: Decimal | None
    unrealised_pnl: Decimal | None
    cum_realised_pnl: Decimal | None


@dataclass(frozen=True, slots=True)
class WalletSnapshot:
    account_type: str
    total_equity: Decimal | None
    total_wallet_balance: Decimal | None
    total_margin_balance: Decimal | None
    total_available_balance: Decimal | None
    total_perp_upl: Decimal | None
    coins: tuple[WalletCoin, ...]


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    symbol: str
    side: str
    size: Decimal
    avg_price: Decimal | None
    position_value: Decimal | None
    leverage: Decimal | None
    mark_price: Decimal | None
    liq_price: Decimal | None
    unrealised_pnl: Decimal | None
    cum_realised_pnl: Decimal | None
    position_im: Decimal | None
    position_mm: Decimal | None
    take_profit: Decimal | None
    stop_loss: Decimal | None
    trailing_stop: Decimal | None
    updated_time_ms: int | None

    @property
    def is_open(self) -> bool:
        return self.size > 0 and self.side in {"Buy", "Sell"}


@dataclass(frozen=True, slots=True)
class OrderSnapshot:
    order_id: str
    order_link_id: str
    symbol: str
    side: str
    order_status: str
    order_type: str
    time_in_force: str
    price: Decimal | None
    qty: Decimal
    avg_price: Decimal | None
    leaves_qty: Decimal | None
    cum_exec_qty: Decimal | None
    cum_exec_value: Decimal | None
    cum_exec_fee: Decimal | None
    trigger_price: Decimal | None
    take_profit: Decimal | None
    stop_loss: Decimal | None
    reduce_only: bool
    close_on_trigger: bool
    created_time_ms: int | None
    updated_time_ms: int | None


def parse_wallet_coin(item: dict[str, object]) -> WalletCoin:
    return WalletCoin(
        coin=str(item.get("coin", "")),
        equity=decimal_required(item.get("equity"), "wallet.coin.equity"),
        wallet_balance=decimal_required(
            item.get("walletBalance"), "wallet.coin.walletBalance"
        ),
        usd_value=decimal_optional(item.get("usdValue")),
        unrealised_pnl=decimal_optional(item.get("unrealisedPnl")),
        cum_realised_pnl=decimal_optional(item.get("cumRealisedPnl")),
    )


def parse_wallet_snapshot(item: dict[str, object]) -> WalletSnapshot:
    coins_raw = item.get("coin") or []
    coins = tuple(
        parse_wallet_coin(coin)
        for coin in coins_raw
        if isinstance(coin, dict)
    )
    return WalletSnapshot(
        account_type=str(item.get("accountType", "")),
        total_equity=decimal_optional(item.get("totalEquity")),
        total_wallet_balance=decimal_optional(item.get("totalWalletBalance")),
        total_margin_balance=decimal_optional(item.get("totalMarginBalance")),
        total_available_balance=decimal_optional(item.get("totalAvailableBalance")),
        total_perp_upl=decimal_optional(item.get("totalPerpUPL")),
        coins=coins,
    )


def parse_position_snapshot(item: dict[str, object]) -> PositionSnapshot:
    updated_time = item.get("updatedTime")
    return PositionSnapshot(
        symbol=str(item.get("symbol", "")),
        side=str(item.get("side", "")),
        size=decimal_required(item.get("size"), "position.size"),
        avg_price=decimal_optional(item.get("avgPrice")),
        position_value=decimal_optional(item.get("positionValue")),
        leverage=decimal_optional(item.get("leverage")),
        mark_price=decimal_optional(item.get("markPrice")),
        liq_price=decimal_optional(item.get("liqPrice")),
        unrealised_pnl=decimal_optional(item.get("unrealisedPnl")),
        cum_realised_pnl=decimal_optional(item.get("cumRealisedPnl")),
        position_im=decimal_optional(item.get("positionIM")),
        position_mm=decimal_optional(item.get("positionMM")),
        take_profit=decimal_optional(item.get("takeProfit")),
        stop_loss=decimal_optional(item.get("stopLoss")),
        trailing_stop=decimal_optional(item.get("trailingStop")),
        updated_time_ms=int(updated_time) if updated_time not in (None, "") else None,
    )


def parse_order_snapshot(item: dict[str, object]) -> OrderSnapshot:
    created_time = item.get("createdTime")
    updated_time = item.get("updatedTime")
    return OrderSnapshot(
        order_id=str(item.get("orderId", "")),
        order_link_id=str(item.get("orderLinkId", "")),
        symbol=str(item.get("symbol", "")),
        side=str(item.get("side", "")),
        order_status=str(item.get("orderStatus", "")),
        order_type=str(item.get("orderType", "")),
        time_in_force=str(item.get("timeInForce", "")),
        price=decimal_optional(item.get("price")),
        qty=decimal_required(item.get("qty"), "order.qty"),
        avg_price=decimal_optional(item.get("avgPrice")),
        leaves_qty=decimal_optional(item.get("leavesQty")),
        cum_exec_qty=decimal_optional(item.get("cumExecQty")),
        cum_exec_value=decimal_optional(item.get("cumExecValue")),
        cum_exec_fee=decimal_optional(item.get("cumExecFee")),
        trigger_price=decimal_optional(item.get("triggerPrice")),
        take_profit=decimal_optional(item.get("takeProfit")),
        stop_loss=decimal_optional(item.get("stopLoss")),
        reduce_only=bool(item.get("reduceOnly", False)),
        close_on_trigger=bool(item.get("closeOnTrigger", False)),
        created_time_ms=int(created_time) if created_time not in (None, "") else None,
        updated_time_ms=int(updated_time) if updated_time not in (None, "") else None,
    )
