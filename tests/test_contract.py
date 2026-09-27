"""The new contract has one route per operation; retired inputs never execute."""

import json

import pytest
from jsonschema import Draft202012Validator
from test_server import call

from hosted_hyperliquid.catalog import CATALOG


@pytest.mark.parametrize(
    "name",
    [
        "get_balance",
        "get_positions",
        "get_account_info",
        "get_meta",
        "get_all_mids",
        "get_server_time",
        "get_open_orders",
        "get_order_history",
        "place_order",
        "modify_order",
        "cancel_order",
    ],
)
def test_removed_names(client, wallet, fake_sdk, name):
    assert call(client, wallet, "hyperliquid_" + name, {})["isError"]
    fake_sdk.info_factory.assert_not_called()
    fake_sdk.exchange_factory.assert_not_called()


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_account", {"scope": "perp"}),
        ("get_account", {"sections": ["mode"]}),
        ("get_orders", {"view": "history", "dex": "xyz"}),
        ("get_user_fills", {"startTime": 0}),
        ("get_order_book", {"coin": "ETH"}),
        ("place_orders", {"asset": 0, "size": "1", "isBuy": True}),
        ("place_orders", {"orders": []}),
        (
            "place_orders",
            {
                "orders": [
                    {"marketId": "perp:ETH", "type": "market", "size": "1", "isBuy": True, "price": "0"}
                ]
            },
        ),
        ("place_orders", {"orders": [{"marketId": "perp:ETH", "type": "limit", "size": "1", "isBuy": True}]}),
        ("update_leverage", {"marketId": "perp:ETH", "leverage": 2}),
        ("cancel_all_orders", {}),
    ],
)
def test_removed_and_ambiguous_inputs(client, wallet, fake_sdk, name, args):
    assert call(client, wallet, "hyperliquid_" + name, args)["isError"]
    fake_sdk.info_factory.assert_not_called()
    fake_sdk.exchange_factory.assert_not_called()


@pytest.mark.parametrize("market", ["perp:ETH", "perp:xyz:SP500", "spot:@1"])
@pytest.mark.parametrize(
    "name,extra,kind",
    [
        ("get_order_book", {}, "l2Book"),
        ("get_candles", {"startTime": 0, "endTime": 1, "interval": "1m"}, "candleSnapshot"),
    ],
)
def test_exact_read_symbols(client, wallet, trading, market, name, extra, kind):
    trading.info.post.return_value = {"levels": [[], []]} if kind == "l2Book" else []
    result = call(client, wallet, "hyperliquid_" + name, {"marketId": market, **extra})
    assert not result.get("isError"), result
    payload = trading.info.post.call_args.args[1]
    assert payload["type"] == kind
    assert payload.get("req", payload)["coin"] == market.split(":", 1)[1]
    trading.exchange_factory.assert_not_called()


def test_funding_builder_and_spot_rejection(client, wallet, trading):
    trading.info.post.return_value = []
    assert not call(
        client, wallet, "hyperliquid_get_historical_funding", {"marketId": "perp:xyz:SP500", "startTime": 0}
    ).get("isError")
    assert trading.info.post.call_args.args[1]["coin"] == "xyz:SP500"
    trading.info.post.reset_mock()
    assert call(
        client, wallet, "hyperliquid_get_historical_funding", {"marketId": "spot:@1", "startTime": 0}
    )["isError"]
    trading.info.post.assert_not_called()


def test_account_freshness_and_precision(client, wallet, fake_sdk):
    for amount in ["9007199254740993.123456", "0.000001"]:
        fake_sdk.info.user_state.return_value["withdrawable"] = amount
        response = call(client, wallet, "hyperliquid_get_account", {})
        assert (
            json.loads(response["content"][0]["text"])["balanceLocations"]["perps"][0]["reportedWithdrawable"]
            == amount
        )
    assert fake_sdk.info.user_state.call_count == 2
    assert fake_sdk.info.session.close.call_count == 2


@pytest.mark.parametrize("bad", ["NaN", "Infinity", 1.0, None, "1e3"])
def test_account_bad_decimals_fail_closed(client, wallet, fake_sdk, bad):
    fake_sdk.info.user_state.return_value["withdrawable"] = bad
    assert call(client, wallet, "hyperliquid_get_account", {})["isError"]


def test_catalog_schemas():
    assert len({tool["name"] for tool in CATALOG}) == len(CATALOG) == 30
    for tool in CATALOG:
        Draft202012Validator.check_schema(tool["inputSchema"])
