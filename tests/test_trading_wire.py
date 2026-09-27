"""Exercise the real SDK and signing, intercepting HTTP before any network access."""

import json
from unittest.mock import Mock

import pytest
from test_server import call


@pytest.fixture
def wire(monkeypatch):
    import requests

    sent = []
    calls = []

    def post(self, url, *, json=None, **kwargs):
        calls.append((url, json))
        if url.endswith("/exchange"):
            sent.append(json)
            action = json["action"]
            if action["type"] in {
                "updateLeverage",
                "updateIsolatedMargin",
                "scheduleCancel",
                "sendAsset",
                "userSetAbstraction",
            }:
                response = {"type": "default"}
            else:
                count = len(action.get("orders", action.get("modifies", action.get("cancels", []))))
                response = {"type": action["type"], "data": {"statuses": ["success"] * count}}
            return Mock(status_code=200, json=lambda: {"status": "ok", "response": response})
        kind = json["type"]
        dex = json.get("dex", "")
        if kind == "meta":
            data = {
                "universe": [{"name": dex + ":SP500" if dex else "ETH", "szDecimals": 3, "maxLeverage": 25}],
                "collateralToken": 0,
            }
        elif kind == "spotMeta":
            data = {
                "tokens": [
                    {"name": "USDC", "index": 0, "szDecimals": 8, "weiDecimals": 8, "tokenId": "0x123"},
                    {"name": "HYPE", "index": 5, "szDecimals": 2},
                ],
                "universe": [{"name": "@1", "index": 1, "tokens": [5, 0]}],
            }
        elif kind == "perpDexs":
            data = [None, {"name": "xyz", "fullName": "XYZ"}]
        elif kind == "allMids":
            data = {"xyz:SP500": "7731.2", "ETH": "2000", "@1": "40"}
        elif kind == "clearinghouseState":
            data = {
                "assetPositions": [
                    {
                        "position": {
                            "coin": "xyz:SP500",
                            "szi": "-0.01",
                            "leverage": {"type": "isolated", "value": 10},
                        }
                    }
                ]
            }
        else:
            raise AssertionError(kind)
        return Mock(status_code=200, json=lambda: data)

    monkeypatch.setattr(requests.Session, "post", post)
    return (sent, calls)


@pytest.mark.parametrize(
    "market,size,price,asset",
    [
        ("perp:xyz:SP500", "0.01", "7731.2", 110000),
        ("perp:ETH", "0.1", "2000", 0),
        ("spot:@1", "1", "40", 10001),
    ],
)
def test_real_sdk_signs_correct_asset_without_live_submission(
    client, wallet, wire, market, size, price, asset
):
    r = call(
        client,
        wallet,
        "hyperliquid_place_orders",
        {"orders": [{"marketId": market, "size": size, "price": price, "isBuy": True, "type": "limit"}]},
    )
    assert not r.get("isError", False), r
    sent, calls = wire
    assert len(sent) == 1
    assert sent[0]["action"]["orders"][0]["a"] == asset
    assert sent[0]["action"]["orders"][0]["s"] == size
    assert {"r", "s", "v"} == set(sent[0]["signature"])
    assert wallet.key.hex() not in json.dumps(calls)


def test_real_sdk_builder_leverage_asset(client, wallet, wire):
    r = call(
        client,
        wallet,
        "hyperliquid_update_leverage",
        {"marketId": "perp:xyz:SP500", "leverage": 10, "isCross": True},
    )
    assert not r.get("isError", False), r
    assert wire[0][0]["action"] == {
        "type": "updateLeverage",
        "asset": 110000,
        "isCross": True,
        "leverage": 10,
    }


def test_real_sdk_client_id_cancel(client, wallet, wire):
    cid = "0x" + "a" * 32
    r = call(
        client,
        wallet,
        "hyperliquid_cancel_orders",
        {"orders": [{"marketId": "perp:xyz:SP500", "cloid": cid}]},
    )
    assert not r.get("isError", False), r
    assert wire[0][0]["action"] == {"type": "cancelByCloid", "cancels": [{"asset": 110000, "cloid": cid}]}


@pytest.mark.parametrize(
    "name,args,action_type",
    [
        (
            "modify_orders",
            {
                "orders": [
                    {
                        "marketId": "perp:xyz:SP500",
                        "cloid": "0x" + "a" * 32,
                        "size": "0.01",
                        "type": "limit",
                        "price": "7731.2",
                        "isBuy": True,
                    }
                ]
            },
            "batchModify",
        ),
        ("close_position", {"marketId": "perp:xyz:SP500"}, "order"),
        (
            "update_isolated_margin",
            {"marketId": "perp:xyz:SP500", "amount": "-0.123456"},
            "updateIsolatedMargin",
        ),
        ("schedule_cancel", {"time": None}, "scheduleCancel"),
    ],
)
def test_new_writes_real_sdk_wire(client, wallet, wire, name, args, action_type):
    result = call(client, wallet, "hyperliquid_" + name, args)
    assert not result.get("isError", False), result
    assert len(wire[0]) == 1
    action = wire[0][0]["action"]
    assert action["type"] == action_type
    if name == "update_isolated_margin":
        assert action["ntli"] == -123456 and action["asset"] == 110000
    if name == "modify_orders":
        assert action["modifies"][0]["order"]["a"] == 110000
    if name == "close_position":
        assert action["orders"][0]["r"] is True and action["orders"][0]["b"] is True


def test_exchange_metadata_failure_closes_both_sessions(monkeypatch, wallet):
    import requests

    from hosted_hyperliquid.sdk_info import RequestExchange

    exchange_session, info_session = (Mock(), Mock())
    info_session.post.side_effect = TimeoutError("unavailable")
    monkeypatch.setattr(requests, "Session", Mock(side_effect=[exchange_session, info_session]))
    with pytest.raises(TimeoutError):
        RequestExchange(wallet, perp_dexs=["", "xyz"])
    exchange_session.close.assert_called_once()
    info_session.close.assert_called_once()


@pytest.mark.parametrize("source,destination", [("", "spot"), ("spot", "xyz"), ("xyz", "")])
def test_transfer_real_sdk_signed_payload(client, wallet, wire, monkeypatch, source, destination):
    from hosted_hyperliquid import collateral

    monkeypatch.setattr(collateral, "collateral_snapshot", lambda *a: {"accountMode": "disabled"})
    r = call(
        client,
        wallet,
        "hyperliquid_transfer_collateral",
        {"source": source, "destination": destination, "tokenIndex": 0, "amount": "9.903796"},
    )
    assert not r.get("isError"), r
    assert len(wire[0]) == 1
    payload = wire[0][0]
    action = payload["action"]
    assert action["type"] == "sendAsset"
    assert action["sourceDex"] == source and action["destinationDex"] == destination
    assert action["destination"] == wallet.address and action["fromSubAccount"] == ""
    assert action["token"] == "USDC:0x123"
    assert action["amount"] == "9.903796" and action["hyperliquidChain"] == "Testnet"
    assert payload["nonce"] == action["nonce"]
    assert len(wire[1]) == 3 + len({source, destination} - {"spot"})
    assert set(payload["signature"]) == {"r", "s", "v"}
    assert wallet.key.hex() not in json.dumps(wire[1])


@pytest.mark.parametrize("requested,target", [("unified", "unifiedAccount"), ("standard", "disabled")])
def test_account_mode_real_sdk_signed_payload(client, wallet, wire, monkeypatch, requested, target):
    from hosted_hyperliquid import collateral

    states = iter(["default", target])
    monkeypatch.setattr(collateral, "mode", lambda *args: next(states))
    r = call(client, wallet, "hyperliquid_set_account_mode", {"mode": requested})
    assert not r.get("isError"), r
    assert len(wire[0]) == 1
    payload = wire[0][0]
    action = payload["action"]
    assert action["type"] == "userSetAbstraction"
    assert action["user"] == wallet.address.lower()
    assert action["abstraction"] == target
    assert action["hyperliquidChain"] == "Testnet"
    assert action["nonce"] == payload["nonce"]
    assert len(wire[1]) == 1
    assert set(payload["signature"]) == {"r", "s", "v"}
    assert wallet.key.hex() not in json.dumps(wire[1])
    assert json.loads(r["content"][0]["text"])["verification"]["status"] == "verified"


@pytest.mark.parametrize(
    "name,args",
    [
        ("schedule_cancel", {"time": None}),
        ("withdraw", {"amount": "1"}),
    ],
)
def test_non_market_writes_do_not_load_metadata(client, wallet, wire, name, args):
    call(client, wallet, "hyperliquid_" + name, args)
    assert len(wire[0]) == 1
    assert len(wire[1]) == 1 and wire[1][0][0].endswith("/exchange")
