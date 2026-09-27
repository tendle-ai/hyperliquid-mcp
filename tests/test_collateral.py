import copy
import json
from unittest.mock import Mock

import pytest
from test_server import call

from hosted_hyperliquid import tools


def decode(result):
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def transfer(fake_sdk):
    fake_sdk.exchange.send_asset.return_value = {"status": "ok", "response": {"type": "default"}}
    fake_sdk.info.perp_dexs.return_value = [
        None,
        {"name": "xyz", "fullName": "XYZ"},
        {"name": "other", "fullName": "Other"},
    ]
    fake_sdk.info.spot_meta.return_value = {
        "tokens": [{"name": "USDC", "index": 0, "szDecimals": 8, "weiDecimals": 8, "tokenId": "0x123"}]
    }
    fake_sdk.info.meta.return_value = {"collateralToken": 0}
    return fake_sdk


def args(source="", destination="spot", amount="1"):
    return {"source": source, "destination": destination, "tokenIndex": 0, "amount": amount}


@pytest.mark.parametrize(
    "source,destination",
    [
        ("", "spot"),
        ("spot", ""),
        ("", "xyz"),
        ("xyz", ""),
        ("spot", "xyz"),
        ("xyz", "spot"),
        ("xyz", "other"),
    ],
)
def test_transfer_directions_precision_and_reads(client, wallet, transfer, source, destination):
    r = decode(call(client, wallet, "hyperliquid_transfer_collateral", args(source, destination, "9.903796")))
    transfer.exchange.send_asset.assert_called_once_with(
        wallet.address, source, destination, "USDC:0x123", "9.903796"
    )
    assert r["status"] == "submitted" and r["verification"]["status"] == "balances_observed"
    assert not r["verification"]["transferConfirmed"]
    assert transfer.info.user_state.call_count == 2 * len({source, destination} - {"spot"})
    assert set(r["before"]["balances"]) == {source, destination}
    assert set(r["after"]["balances"]) == {source, destination}


@pytest.mark.parametrize(
    "override",
    [
        {"amount": "0"},
        {"amount": "-1"},
        {"amount": "0.000000001"},
        {"amount": 1},
        {"destination": "missing"},
        {"destination": ""},
        {"tokenIndex": 99},
        {"tokenIndex": True},
        {"recipient": "0x" + "1" * 40},
        {"amount": "NaN"},
    ],
)
def test_transfer_invalid_no_submission(client, wallet, transfer, override):
    assert call(client, wallet, "hyperliquid_transfer_collateral", {**args(), **override})["isError"]
    transfer.exchange_factory.assert_not_called()


@pytest.mark.parametrize("field,value", [("tokenId", None), ("weiDecimals", True), ("weiDecimals", -1)])
def test_bad_token_metadata(client, wallet, transfer, field, value):
    transfer.info.spot_meta.return_value["tokens"][0][field] = value
    assert call(client, wallet, "hyperliquid_transfer_collateral", args())["isError"]
    transfer.exchange_factory.assert_not_called()


def test_collateral_mismatch(client, wallet, transfer):
    transfer.info.meta.side_effect = lambda dex: {"collateralToken": 1 if dex == "xyz" else 0}
    assert call(client, wallet, "hyperliquid_transfer_collateral", args("", "xyz"))["isError"]
    transfer.exchange_factory.assert_not_called()


def test_transfer_requires_auth(client, transfer):
    assert call(client, None, "hyperliquid_transfer_collateral", args())["isError"]
    transfer.info_factory.assert_not_called()


@pytest.mark.parametrize("vault", [False, True])
def test_transfer_owner_only(wallet, transfer, vault):
    from hosted_hyperliquid.auth import WalletIdentity

    h = tools.HyperliquidTools(
        WalletIdentity(
            wallet, "0x" + "1" * 40 if not vault else wallet.address, "0x" + "2" * 40 if vault else None
        ),
        "https://api.hyperliquid-testnet.xyz",
    )
    with pytest.raises(ValueError, match="owner"):
        h.execute("hyperliquid_transfer_collateral", args())
    transfer.info_factory.assert_not_called()
    transfer.exchange_factory.assert_not_called()


@pytest.mark.parametrize("outcome", ["timeout", "rejected", "malformed"])
def test_transfer_uncertain_never_retries(client, wallet, transfer, outcome):
    if outcome == "timeout":
        transfer.exchange.send_asset.side_effect = TimeoutError(wallet.key.hex())
    else:
        transfer.exchange.send_asset.return_value = (
            {"status": "err", "response": "not enough"} if outcome == "rejected" else {}
        )
    r = call(client, wallet, "hyperliquid_transfer_collateral", args())
    assert r["isError"] and wallet.key.hex() not in str(r)
    data = json.loads(r["content"][0]["text"])
    assert data["status"] == ("rejected" if outcome == "rejected" else "unknown")
    transfer.exchange.send_asset.assert_called_once()


def test_post_read_failure_preserves_submission(client, wallet, transfer):
    transfer.info.query_user_abstraction_state.side_effect = ["disabled", TimeoutError(wallet.key.hex())]
    r = decode(call(client, wallet, "hyperliquid_transfer_collateral", args()))
    assert r["status"] == "submitted" and r["verification"]["status"] == "read_failed_do_not_resubmit"
    assert wallet.key.hex() not in str(r)
    transfer.exchange.send_asset.assert_called_once()


def test_pre_read_failure_prevents_submission(client, wallet, transfer):
    transfer.info.query_user_abstraction_state.side_effect = TimeoutError()
    assert call(client, wallet, "hyperliquid_transfer_collateral", args())["isError"]
    transfer.exchange_factory.assert_not_called()


def test_separate_locations(client, wallet, fake_sdk):
    default = copy.deepcopy(fake_sdk.info.user_state.return_value)
    default["marginSummary"]["accountValue"] = "9.903796"
    selected = copy.deepcopy(default)
    selected["marginSummary"]["accountValue"] = "0"
    fake_sdk.info.user_state.side_effect = lambda address, dex: selected if dex else default
    r = decode(call(client, wallet, "hyperliquid_get_account", {"dex": "xyz"}))["balanceLocations"]
    assert r["perps"][0]["reportedMarginSummary"]["accountValue"] == "9.903796"
    assert r["perps"][1]["reportedMarginSummary"]["accountValue"] == "0"
    assert r["interpretation"] == "separate"


@pytest.fixture
def capacity(monkeypatch, fake_sdk, wallet):
    monkeypatch.setattr(
        tools,
        "read_markets",
        Mock(
            return_value={
                "marketType": "perp",
                "marketId": "perp:xyz:SP500",
                "symbol": "xyz:SP500",
                "collateral": {"name": "USDC", "index": 0},
            }
        ),
    )
    fake_sdk.info.post.return_value = {
        "user": wallet.address,
        "coin": "xyz:SP500",
        "leverage": {"type": "cross", "value": 10},
        "availableToTrade": ["0", "0"],
        "maxTradeSzs": ["0", "0"],
        "markPx": "7733.9",
    }
    return fake_sdk


def test_capacity_routes_exact_user_market(client, wallet, capacity):
    r = decode(
        call(
            client,
            wallet,
            "hyperliquid_market_details",
            {"marketId": "perp:xyz:SP500", "includeAccount": True},
        )
    )["accountTrading"]
    assert r["status"] == "observed" and r["availableToTrade"] == ["0", "0"]
    capacity.info.post.assert_called_once_with(
        "/info", {"type": "activeAssetData", "user": wallet.address, "coin": "xyz:SP500"}
    )
    capacity.exchange_factory.assert_not_called()


def test_capacity_opt_in(client, wallet, capacity):
    r = decode(call(client, wallet, "hyperliquid_market_details", {"marketId": "perp:xyz:SP500"}))
    assert "accountTrading" not in r
    capacity.info_factory.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("availableToTrade", ["NaN", "0"]),
        ("maxTradeSzs", ["0"]),
        ("user", "0x" + "0" * 40),
        ("coin", "BTC"),
        ("leverage", {"type": "cross", "value": True}),
    ],
)
def test_invalid_capacity_fails_closed(client, wallet, capacity, field, value):
    capacity.info.post.return_value[field] = value
    assert call(
        client, wallet, "hyperliquid_market_details", {"marketId": "perp:xyz:SP500", "includeAccount": True}
    )["isError"]


def test_missing_capacity_is_not_zero(client, wallet, capacity):
    capacity.info.post.return_value = None
    r = decode(
        call(
            client,
            wallet,
            "hyperliquid_market_details",
            {"marketId": "perp:xyz:SP500", "includeAccount": True},
        )
    )["accountTrading"]
    assert r["status"] == "unavailable" and "availableToTrade" not in r


def test_spot_does_not_call_perp_capacity(client, wallet, capacity, monkeypatch):
    monkeypatch.setattr(tools, "read_markets", Mock(return_value={"marketType": "spot"}))
    r = decode(
        call(client, wallet, "hyperliquid_market_details", {"marketId": "spot:@1", "includeAccount": True})
    )
    assert r["accountTrading"]["status"] == "not_supported"
    capacity.info_factory.assert_not_called()


@pytest.mark.parametrize("account_mode", ["unifiedAccount", "portfolioMargin"])
@pytest.mark.parametrize("source,destination", [("", "spot"), ("spot", "xyz"), ("", "xyz")])
def test_shared_mode_transfer_not_applicable(client, wallet, transfer, account_mode, source, destination):
    transfer.info.query_user_abstraction_state.return_value = account_mode
    r = decode(call(client, wallet, "hyperliquid_transfer_collateral", args(source, destination)))
    assert r["status"] == "not_applicable" and r["submitted"] is False
    assert r["accountMode"] == account_mode
    transfer.exchange_factory.assert_not_called()
    transfer.exchange.user_set_abstraction.assert_not_called()
    assert transfer.info.query_user_abstraction_state.call_count == 1
