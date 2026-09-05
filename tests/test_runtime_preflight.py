from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_scanner.binance.auth import BinanceDemoCredentials
from crypto_scanner.binance.models import InstrumentInfo, TickerSnapshot, WalletSnapshot
from crypto_scanner.execution_plan import TestnetExecutionArm
from crypto_scanner.runtime_preflight import RuntimePreflightError, run_runtime_preflight


def test_preflight_rejects_armed_runtime_before_credentials(monkeypatch) -> None:
    import crypto_scanner.runtime_preflight as module

    monkeypatch.setattr(
        module.TestnetExecutionArm,
        "from_environment",
        classmethod(lambda cls: TestnetExecutionArm(True)),
    )
    monkeypatch.setattr(
        module.BinanceDemoCredentials,
        "from_environment",
        classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("credentials touched"))),
    )
    with pytest.raises(RuntimePreflightError, match="DISABLED"):
        run_runtime_preflight()


class _PublicClient:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        pass

    def get_instrument(self, symbol: str) -> InstrumentInfo:
        return InstrumentInfo(
            symbol=symbol,
            status="Trading",
            contract_type="PERPETUAL",
            base_coin=symbol.removesuffix("USDT"),
            quote_coin="USDT",
            settle_coin="USDT",
            tick_size=Decimal("0.1"),
            min_order_qty=Decimal("0.001"),
            qty_step=Decimal("0.001"),
            min_notional_value=Decimal("5"),
            max_order_qty=Decimal("100"),
            max_market_order_qty=Decimal("50"),
            min_leverage=None,
            max_leverage=None,
            leverage_step=None,
        )

    def get_ticker(self, symbol: str) -> TickerSnapshot:
        return TickerSnapshot(
            symbol=symbol,
            last_price=Decimal("100"),
            mark_price=Decimal("100"),
            index_price=Decimal("100"),
            bid_price=Decimal("99.9"),
            ask_price=Decimal("100.1"),
            bid_size=Decimal("10"),
            ask_size=Decimal("10"),
            volume_24h=Decimal("1000"),
            turnover_24h=Decimal("100000"),
            open_interest=Decimal("500"),
            open_interest_value=None,
            funding_rate=Decimal("0.0001"),
            next_funding_time_ms=None,
        )


class _PrivateClient:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        pass

    def get_wallet_balance(self) -> WalletSnapshot:
        return WalletSnapshot(
            account_type="FUTURES_DEMO",
            total_equity=Decimal("1000"),
            total_wallet_balance=Decimal("1000"),
            total_margin_balance=Decimal("1000"),
            total_available_balance=Decimal("900"),
            total_perp_upl=Decimal("0"),
            coins=(),
        )

    def get_positions(self):
        return ()

    def get_open_orders(self):
        return ()


def test_preflight_passes_only_disarmed_public_and_private_reads(monkeypatch) -> None:
    import crypto_scanner.runtime_preflight as module

    monkeypatch.setattr(
        module.TestnetExecutionArm,
        "from_environment",
        classmethod(lambda cls: TestnetExecutionArm(False)),
    )
    monkeypatch.setattr(
        module.BinanceDemoCredentials,
        "from_environment",
        classmethod(lambda cls: BinanceDemoCredentials("key", "secret")),
    )
    monkeypatch.setattr(module, "BinanceDemoPublicRestClient", _PublicClient)
    monkeypatch.setattr(module, "BinanceDemoPrivateReadOnlyClient", _PrivateClient)
    report = run_runtime_preflight()
    assert report["venue"] == "BINANCE"
    assert report["environment"] == "DEMO"
    assert report["preflight_status"] == "PASS_DISARMED"
    assert report["testnet_execution_armed"] is False
    assert report["private_account"]["total_equity"] == "1000"
    assert set(report["public_symbols"]) == {
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "BNBUSDT",
    }
    assert "secret" not in str(report).lower()
