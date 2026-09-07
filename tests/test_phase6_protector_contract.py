from crypto_scanner.phase6_audit import _protector_exchange_contract_rows


def _algo(
    *,
    order_type: str,
    working_type: str = "MARK_PRICE",
    position_side: str = "BOTH",
    reduce_only: bool = True,
) -> dict[str, object]:
    return {
        "algoId": f"id-{order_type}",
        "clientAlgoId": f"client-{order_type}",
        "symbol": "NEARUSDT",
        "side": "BUY",
        "orderType": order_type,
        "algoStatus": "NEW",
        "workingType": working_type,
        "positionSide": position_side,
        "reduceOnly": reduce_only,
    }


def test_active_stop_and_tp2_pass_exact_exchange_contract() -> None:
    rows, blockers = _protector_exchange_contract_rows(
        [_algo(order_type="STOP_MARKET"), _algo(order_type="TAKE_PROFIT_MARKET")]
    )
    assert blockers == []
    assert len(rows) == 2
    assert all(row["contract_ok"] is True for row in rows)
    assert all(row["working_type"] == "MARK_PRICE" for row in rows)
    assert all(row["position_side"] == "BOTH" for row in rows)


def test_wrong_working_type_fails_closed() -> None:
    rows, blockers = _protector_exchange_contract_rows(
        [_algo(order_type="STOP_MARKET", working_type="CONTRACT_PRICE")]
    )
    assert rows[0]["contract_ok"] is False
    assert blockers == [
        "PROTECTOR_EXCHANGE_CONTRACT_INVALID:NEARUSDT:client-STOP_MARKET:STOP_MARKET"
    ]


def test_wrong_position_side_or_reduce_only_fails_closed() -> None:
    rows, blockers = _protector_exchange_contract_rows(
        [
            _algo(order_type="STOP_MARKET", position_side="SHORT"),
            _algo(order_type="TAKE_PROFIT_MARKET", reduce_only=False),
        ]
    )
    assert len(blockers) == 2
    assert all(row["contract_ok"] is False for row in rows)


def test_inactive_or_nonprotector_orders_are_ignored() -> None:
    inactive = _algo(order_type="STOP_MARKET")
    inactive["algoStatus"] = "CANCELED"
    market = _algo(order_type="MARKET")
    rows, blockers = _protector_exchange_contract_rows([inactive, market])
    assert rows == []
    assert blockers == []
