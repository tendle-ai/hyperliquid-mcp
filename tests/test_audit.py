"""Regression checks for the execution-path and response-consistency audit."""

import json

import pytest
from test_server import call

from hosted_hyperliquid.server import exchange_rejected


@pytest.mark.parametrize("flag", ["onlyIsolated", "strictIsolated", "noCross"])
def test_isolated_markets_reject_cross_before_submission(client, wallet, trading, flag):
    asset = {"name": "xyz:SP500", "szDecimals": 3, "maxLeverage": 25}
    asset.update({"onlyIsolated": True} if flag == "onlyIsolated" else {"marginMode": flag})
    trading.info.meta.side_effect = None
    trading.info.meta.return_value = {"universe": [asset]}
    r = call(
        client,
        wallet,
        "hyperliquid_update_leverage",
        {"marketId": "perp:xyz:SP500", "leverage": 10, "isCross": True},
    )
    assert r["isError"]
    trading.exchange_factory.assert_not_called()


def test_leverage_verification_without_position(client, wallet, trading):
    trading.info.user_state.return_value["assetPositions"] = []
    trading.info.post.return_value = {
        "user": wallet.address,
        "coin": "xyz:SP500",
        "leverage": {"type": "cross", "value": 10},
        "availableToTrade": ["0", "0"],
        "maxTradeSzs": ["0", "0"],
        "markPx": "7733",
    }
    r = call(
        client,
        wallet,
        "hyperliquid_update_leverage",
        {"marketId": "perp:xyz:SP500", "leverage": 10, "isCross": True},
    )
    assert json.loads(r["content"][0]["text"])["verification"]["status"] == "verified"
    trading.info.user_state.assert_not_called()


@pytest.mark.parametrize("statuses", [None, {}, [{}], ["unexpected"], [{"resting": {}}]])
def test_malformed_order_acknowledgement_preserves_unknown(client, wallet, trading, statuses):
    trading.exchange.bulk_orders.return_value = {"status": "ok", "response": {"data": {"statuses": statuses}}}
    r = call(
        client,
        wallet,
        "hyperliquid_place_orders",
        {"orders": [{"marketId": "perp:ETH", "type": "limit", "isBuy": True, "size": "1", "price": "2000"}]},
    )
    assert r["isError"]
    d = json.loads(r["content"][0]["text"])
    assert d["data"]["status"] == "unknown" and len(d["clientOrderIds"]) == 1
    trading.exchange.bulk_orders.assert_called_once()


@pytest.mark.parametrize(
    "response", [{"data": None}, {"data": {"statuses": None}}, {"data": {"statuses": 123}}]
)
def test_result_classification_does_not_raise_on_malformed_ack(response):
    assert exchange_rejected(
        {"data": {"status": "unknown", "error": "reconcile", "exchangeResponse": response}}
    )
    assert exchange_rejected({"data": {"response": response}}) is False


def test_default_account_balance_is_not_duplicated(client, wallet, fake_sdk):
    r = call(client, wallet, "hyperliquid_get_account", {})
    d = json.loads(r["content"][0]["text"])
    assert "spot" not in d and "perpMargin" not in d
    assert len(d["balanceLocations"]["perps"]) == 1
    assert d["balanceLocations"]["perps"][0]["dex"] == ""
    fake_sdk.info.user_state.assert_called_once()
