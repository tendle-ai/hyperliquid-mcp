import json

import pytest
from test_server import call

from hosted_hyperliquid.auth import WalletIdentity
from hosted_hyperliquid.tools import HyperliquidTools

TOOL = "hyperliquid_set_account_mode"


def unpack(response):
    return json.loads(response["content"][0]["text"])


@pytest.mark.parametrize("before", ["default", "disabled", "dexAbstraction"])
def test_enable_and_verify(client, wallet, fake_sdk, before):
    fake_sdk.info.query_user_abstraction_state.side_effect = [before, "unifiedAccount"]
    fake_sdk.exchange.user_set_abstraction.return_value = {"status": "ok", "response": {"type": "default"}}
    r = call(client, wallet, TOOL, {"mode": "unified"})
    assert not r.get("isError"), r
    data = unpack(r)
    assert data["beforeMode"] == before and data["verification"]["status"] == "verified"
    assert data["status"] == "submitted" and data["submitted"] is True
    fake_sdk.exchange.user_set_abstraction.assert_called_once_with(wallet.address, "unifiedAccount")
    assert fake_sdk.info.query_user_abstraction_state.call_count == 2
    fake_sdk.exchange.send_asset.assert_not_called()
    fake_sdk.exchange.bulk_orders.assert_not_called()


def test_already_unified_skips_write(client, wallet, fake_sdk):
    fake_sdk.info.query_user_abstraction_state.return_value = "unifiedAccount"
    r = unpack(call(client, wallet, TOOL, {"mode": "unified"}))
    assert r["status"] == "already_configured" and r["submitted"] is False
    assert r["verification"]["status"] == "verified"
    fake_sdk.exchange_factory.assert_not_called()


@pytest.mark.parametrize("before", ["portfolioMargin", "newMode", None])
def test_no_downgrade_or_unknown_mode(client, wallet, fake_sdk, before):
    fake_sdk.info.query_user_abstraction_state.return_value = before
    assert call(client, wallet, TOOL, {"mode": "unified"})["isError"]
    fake_sdk.exchange_factory.assert_not_called()


@pytest.mark.parametrize("after", ["default", TimeoutError("private upstream message")])
def test_readback_does_not_claim_success_or_repeat(client, wallet, fake_sdk, after):
    fake_sdk.info.query_user_abstraction_state.side_effect = ["default", after]
    fake_sdk.exchange.user_set_abstraction.return_value = {"status": "ok", "response": {"type": "default"}}
    r = unpack(call(client, wallet, TOOL, {"mode": "unified"}))
    assert r["status"] == "submitted"
    assert r["verification"]["status"] == (
        "not_verified" if isinstance(after, str) else "read_failed_do_not_resubmit"
    )
    assert "private upstream message" not in str(r)
    fake_sdk.exchange.user_set_abstraction.assert_called_once()


@pytest.mark.parametrize("outcome", ["timeout", "malformed", "rejected"])
def test_ambiguous_or_rejected_action(client, wallet, fake_sdk, outcome):
    fake_sdk.info.query_user_abstraction_state.side_effect = ["default", "unifiedAccount"]
    if outcome == "timeout":
        fake_sdk.exchange.user_set_abstraction.side_effect = TimeoutError(wallet.key.hex())
    else:
        fake_sdk.exchange.user_set_abstraction.return_value = (
            {} if outcome == "malformed" else {"status": "err", "response": "rejected"}
        )
    r = call(client, wallet, TOOL, {"mode": "unified"})
    assert r["isError"] and wallet.key.hex() not in str(r)
    d = unpack(r)
    assert d["status"] == ("rejected" if outcome == "rejected" else "unknown")
    assert d["verification"]["status"] == ("not_checked" if outcome == "rejected" else "verified")
    fake_sdk.exchange.user_set_abstraction.assert_called_once()


def test_pre_read_failure_prevents_submission(client, wallet, fake_sdk):
    fake_sdk.info.query_user_abstraction_state.side_effect = TimeoutError()
    assert call(client, wallet, TOOL, {"mode": "unified"})["isError"]
    fake_sdk.exchange_factory.assert_not_called()


@pytest.mark.parametrize("vault", [False, True])
def test_owner_only(wallet, fake_sdk, vault):
    identity = WalletIdentity(
        wallet, wallet.address if vault else "0x" + "1" * 40, "0x" + "2" * 40 if vault else None
    )
    handler = HyperliquidTools(identity, "https://api.hyperliquid-testnet.xyz")
    with pytest.raises(ValueError, match="owner"):
        handler.execute(TOOL, {"mode": "unified"})
    fake_sdk.info_factory.assert_not_called()
    fake_sdk.exchange_factory.assert_not_called()


def test_auth_and_schema(client, wallet, fake_sdk):
    assert call(client, None, TOOL, {"mode": "unified"})["isError"]
    for args in [{"userAddress": wallet.address}, {"mode": "portfolioMargin"}, {"confirmed": True}]:
        assert call(client, wallet, TOOL, args)["isError"]
    fake_sdk.info_factory.assert_not_called()


def test_switch_to_standard_and_verify(client, wallet, fake_sdk):
    fake_sdk.info.query_user_abstraction_state.side_effect = ["unifiedAccount", "disabled"]
    fake_sdk.exchange.user_set_abstraction.return_value = {"status": "ok", "response": {"type": "default"}}
    r = call(client, wallet, TOOL, {"mode": "standard"})
    assert not r.get("isError"), r
    assert unpack(r)["verification"]["status"] == "verified"
    fake_sdk.exchange.user_set_abstraction.assert_called_once_with(wallet.address, "disabled")
    fake_sdk.exchange.send_asset.assert_not_called()


def test_standard_already_configured(client, wallet, fake_sdk):
    fake_sdk.info.query_user_abstraction_state.return_value = "disabled"
    r = unpack(call(client, wallet, TOOL, {"mode": "standard"}))
    assert r["submitted"] is False and r["status"] == "already_configured"
    fake_sdk.exchange_factory.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"mode": "disabled"},
        {"mode": "unifiedAccount"},
        {"mode": "portfolioMargin"},
        {"mode": "standard", "userAddress": "0x" + "1" * 40},
    ],
)
def test_mode_schema_no_network(client, wallet, fake_sdk, args):
    assert call(client, wallet, TOOL, args)["isError"]
    fake_sdk.info_factory.assert_not_called()


def test_retired_enable_tool_removed(client, wallet, fake_sdk):
    assert call(client, wallet, "hyperliquid_enable_unified_account", {})["isError"]
    fake_sdk.info_factory.assert_not_called()
