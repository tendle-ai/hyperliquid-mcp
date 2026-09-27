import json
import time
from decimal import Decimal

import pytest
from test_server import call


def invoke(client, wallet, name, args):
    result = call(client, wallet, "hyperliquid_" + name, args)
    assert not result.get("isError", False), result
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize(
    "market,size,price",
    [("perp:xyz:SP500", "0.01", "7731.2"), ("perp:ETH", "0.1", "2000"), ("spot:@1", "1", "40")],
)
def test_exact_market_order_routes(client, wallet, trading, market, size, price):
    r = invoke(
        client,
        wallet,
        "place_orders",
        {"orders": [{"marketId": market, "size": size, "price": price, "isBuy": True, "type": "limit"}]},
    )
    order = trading.exchange.bulk_orders.call_args.args[0][0]
    assert order["coin"] == market.split(":", 1)[1]
    assert order["cloid"].to_raw() == r["clientOrderIds"][0]
    assert order["sz"] == float(size)
    assert trading.exchange_factory.call_args.kwargs.get("perp_dexs", [""]) == (
        ["", "xyz"] if "xyz:" in market else None
    )


def test_mixed_market_batch_one_submission(client, wallet, trading):
    trading.exchange.bulk_orders.return_value["response"]["data"]["statuses"] *= 2
    orders = [
        {"marketId": "perp:xyz:SP500", "size": "0.01", "type": "limit", "price": "7731.2", "isBuy": True},
        {"marketId": "spot:@1", "size": "1", "type": "limit", "price": "40", "isBuy": False},
    ]
    invoke(client, wallet, "place_orders", {"orders": orders})
    trading.exchange.bulk_orders.assert_called_once()
    assert len(trading.exchange.bulk_orders.call_args.args[0]) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        {"size": "0.0001"},
        {"price": "7731.21"},
        {"size": "0.001"},
        {"marketId": "perp:xyz:MISSING"},
        {"asset": 0},
        {"reduceOnly": True, "marketId": "spot:@1"},
    ],
)
def test_invalid_order_never_submits(client, wallet, trading, mutation):
    args = {
        "marketId": "perp:xyz:SP500",
        "size": "0.01",
        "type": "limit",
        "price": "7731.2",
        "isBuy": True,
        **mutation,
    }
    assert call(client, wallet, "hyperliquid_place_orders", {"orders": [{**args, "type": "limit"}]})[
        "isError"
    ]
    trading.exchange_factory.assert_not_called()


def test_invalid_second_item_prevents_whole_batch(client, wallet, trading):
    good = {"marketId": "perp:xyz:SP500", "size": "0.01", "type": "limit", "price": "7731.2", "isBuy": True}
    assert call(
        client,
        wallet,
        "hyperliquid_place_orders",
        {"orders": [{**good, "type": "limit"}, {**good, "size": "0.0001", "type": "market"}]},
    )["isError"]
    trading.exchange_factory.assert_not_called()


@pytest.mark.parametrize("is_buy", [True, False])
def test_builder_slippage_rounds_inside_bound(client, wallet, trading, is_buy):
    invoke(
        client,
        wallet,
        "place_orders",
        {
            "orders": [
                {
                    "marketId": "perp:xyz:SP500",
                    "size": "0.01",
                    "isBuy": is_buy,
                    "slippage": 0.001,
                    "type": "market",
                }
            ]
        },
    )
    o = trading.exchange.bulk_orders.call_args.args[0][0]
    px = Decimal(str(o["limit_px"]))
    mid = Decimal("7731.2")
    assert mid <= px <= mid * Decimal("1.001") if is_buy else mid * Decimal(".999") <= px <= mid
    assert len(px.normalize().as_tuple().digits) <= 5
    trading.exchange._slippage_price.assert_not_called()


def test_unknown_order_returns_ids_without_retry(client, wallet, trading, caplog):
    trading.exchange.bulk_orders.side_effect = TimeoutError(wallet.key.hex())
    result = call(
        client,
        wallet,
        "hyperliquid_place_orders",
        {
            "orders": [
                {"marketId": "perp:ETH", "size": "0.1", "price": "2000", "isBuy": True, "type": "limit"}
            ]
        },
    )
    data = json.loads(result["content"][0]["text"])
    assert data["data"]["status"] == "unknown" and len(data["clientOrderIds"]) == 1
    assert wallet.key.hex() not in json.dumps(result) + caplog.text
    trading.exchange.bulk_orders.assert_called_once()


def test_partial_batch_is_error_with_all_statuses(client, wallet, trading):
    statuses = [{"filled": {"oid": 1, "totalSz": "0.1", "avgPx": "2000"}}, {"error": "rejected"}]
    trading.exchange.bulk_orders.return_value = {"status": "ok", "response": {"data": {"statuses": statuses}}}
    a = {"marketId": "perp:ETH", "size": "0.1", "type": "limit", "price": "2000", "isBuy": True}
    r = call(
        client,
        wallet,
        "hyperliquid_place_orders",
        {"orders": [{**a, "type": "limit"}, {**a, "type": "limit"}]},
    )
    assert (
        r["isError"]
        and json.loads(r["content"][0]["text"])["data"]["response"]["data"]["statuses"] == statuses
    )


def test_close_short_full_and_partial(client, wallet, trading):
    for size in [None, "0.005"]:
        a = {"marketId": "perp:xyz:SP500"}
        if size:
            a["size"] = size
        invoke(client, wallet, "close_position", a)
        order = trading.exchange.bulk_orders.call_args.args[0][0]
        assert order["is_buy"] and order["reduce_only"]
        assert order["sz"] == float(size or "0.01")


@pytest.mark.parametrize(
    "args",
    [{"marketId": "spot:@1"}, {"marketId": "perp:ETH"}, {"marketId": "perp:xyz:SP500", "size": "0.02"}],
)
def test_close_invalid_never_submits(client, wallet, trading, args):
    assert call(client, wallet, "hyperliquid_close_position", args)["isError"]
    trading.exchange_factory.assert_not_called()


def test_leverage_limits_and_verification(client, wallet, trading):
    args = {"marketId": "perp:xyz:SP500", "leverage": 10, "isCross": False}
    trading.info.post.return_value = {
        "user": wallet.address,
        "coin": "xyz:SP500",
        "leverage": {"type": "isolated", "value": 10},
        "availableToTrade": ["0", "0"],
        "maxTradeSzs": ["0", "0"],
        "markPx": "7731.2",
    }
    result = invoke(client, wallet, "update_leverage", args)
    assert result["verification"]["status"] == "verified"
    trading.exchange.update_leverage.assert_called_once_with(10, "xyz:SP500", is_cross=False)
    assert call(client, wallet, "hyperliquid_update_leverage", {**args, "leverage": 26})["isError"]
    assert trading.exchange.update_leverage.call_count == 1


def test_leverage_read_failure_does_not_hide_acceptance(client, wallet, trading):
    trading.info.post.side_effect = TimeoutError("secret")
    result = invoke(
        client, wallet, "update_leverage", {"marketId": "perp:xyz:SP500", "leverage": 10, "isCross": False}
    )
    assert (
        result["data"]["status"] == "ok" and result["verification"]["status"] == "read_failed_do_not_resubmit"
    )
    trading.exchange.update_leverage.assert_called_once()


@pytest.mark.parametrize("amount", ["1", "-0.5"])
def test_isolated_margin_signed(client, wallet, trading, amount):
    invoke(client, wallet, "update_isolated_margin", {"marketId": "perp:xyz:SP500", "amount": amount})
    trading.exchange.update_isolated_margin.assert_called_once_with(float(amount), "xyz:SP500")


def test_margin_cross_rejected(client, wallet, trading):
    trading.info.user_state.return_value["assetPositions"][0]["position"]["leverage"]["type"] = "cross"
    assert call(
        client, wallet, "hyperliquid_update_isolated_margin", {"marketId": "perp:xyz:SP500", "amount": "1"}
    )["isError"]
    trading.exchange_factory.assert_not_called()


def test_schedule_clear_and_future(client, wallet, trading):
    invoke(client, wallet, "schedule_cancel", {"time": None})
    trading.exchange.schedule_cancel.assert_called_with(None)
    future = int(time.time() * 1000) + 60000
    invoke(client, wallet, "schedule_cancel", {"time": future})
    trading.exchange.schedule_cancel.assert_called_with(future)
    assert call(client, wallet, "hyperliquid_schedule_cancel", {"time": 0})["isError"]


def test_cloid_lookup_cancel_modify(client, wallet, trading):
    cid = "0x" + "a" * 32
    invoke(client, wallet, "get_order_status", {"cloid": cid})
    assert trading.info.query_order_by_cloid.call_args.args[1].to_raw() == cid
    invoke(client, wallet, "cancel_orders", {"orders": [{"marketId": "perp:xyz:SP500", "cloid": cid}]})
    assert trading.exchange.bulk_cancel_by_cloid.call_args.args[0][0]["cloid"].to_raw() == cid
    invoke(
        client,
        wallet,
        "modify_orders",
        {
            "orders": [
                {
                    "marketId": "perp:xyz:SP500",
                    "cloid": cid,
                    "size": "0.01",
                    "price": "7731.2",
                    "isBuy": True,
                    "type": "limit",
                }
            ]
        },
    )
    assert trading.exchange.bulk_modify_orders_new.call_args.args[0][0]["oid"].to_raw() == cid


def test_bracket_builder(client, wallet, trading):
    trading.exchange.bulk_orders.return_value["response"]["data"]["statuses"] *= 3
    invoke(
        client,
        wallet,
        "place_bracket_order",
        {
            "entry": {
                "marketId": "perp:xyz:SP500",
                "type": "limit",
                "size": "0.01",
                "isBuy": True,
                "price": "7700",
            },
            "takeProfitPrice": "7900",
            "stopLossPrice": "7500",
        },
    )
    orders = trading.exchange.bulk_orders.call_args.args[0]
    assert len(orders) == 3 and orders[1]["reduce_only"] and orders[2]["reduce_only"]
    assert trading.exchange.bulk_orders.call_args.kwargs["grouping"] == "normalTpsl"


def test_cancel_all_builder_configures_sdk(client, wallet, trading):
    trading.info.open_orders.return_value = [{"coin": "xyz:SP500", "oid": 1}]
    invoke(client, wallet, "cancel_all_orders", {"dex": "xyz"})
    assert trading.exchange_factory.call_args.kwargs["perp_dexs"] == ["", "xyz"]


@pytest.mark.parametrize(
    "response", [{}, {"status": "ok"}, {"status": "ok", "response": {"data": {"statuses": []}}}]
)
def test_incomplete_ack_is_unknown_not_success(client, wallet, trading, response):
    trading.exchange.bulk_orders.return_value = response
    result = call(
        client,
        wallet,
        "hyperliquid_place_orders",
        {
            "orders": [
                {"marketId": "perp:ETH", "size": "0.1", "price": "2000", "isBuy": True, "type": "limit"}
            ]
        },
    )
    assert result["isError"]
    assert json.loads(result["content"][0]["text"])["data"]["status"] == "unknown"
    trading.exchange.bulk_orders.assert_called_once()


def test_cancel_mixed_identifier_batch_rejected(client, wallet, trading):
    orders = [{"marketId": "perp:ETH", "oid": 1}, {"marketId": "perp:ETH", "cloid": "0x" + "a" * 32}]
    assert call(client, wallet, "hyperliquid_cancel_orders", {"orders": orders})["isError"]
    trading.exchange_factory.assert_not_called()


def test_spot_non_usd_price_not_treated_as_dollars(client, wallet, trading):
    invoke(
        client,
        wallet,
        "place_orders",
        {"orders": [{"marketId": "spot:@1", "size": "1", "price": "0.1", "isBuy": True, "type": "limit"}]},
    )
    trading.exchange.bulk_orders.assert_called_once()
