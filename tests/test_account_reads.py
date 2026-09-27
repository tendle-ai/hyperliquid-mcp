import json

import pytest
from test_server import call


@pytest.fixture
def account(fake_sdk):
    i = fake_sdk.info
    i.query_user_abstraction_state.return_value = "unifiedAccount"
    i.spot_user_state.return_value = {
        "balances": [{"coin": "USDC", "token": 0, "total": "9.903796", "hold": "1.2"}]
    }
    i.user_fills.return_value = []
    i.user_twap_slice_fills.return_value = []
    i.user_vault_equities.return_value = []
    i.user_role.return_value = {"role": "user"}
    i.user_fees.return_value = {"userCrossRate": "0.00045"}
    i.query_sub_accounts.return_value = None
    i.extra_agents.return_value = []
    i.query_user_to_multi_sig_signers.return_value = None
    i.query_referral_state.return_value = {"referredBy": None}
    i.user_rate_limit.return_value = {"nRequestsUsed": 0}
    i.query_user_dex_abstraction_state.return_value = False
    return fake_sdk


def result(client, wallet, name, args):
    r = call(client, wallet, "hyperliquid_" + name, args)
    assert not r.get("isError", False), r
    return json.loads(r["content"][0]["text"])


@pytest.mark.parametrize(
    "mode", ["unifiedAccount", "portfolioMargin", "disabled", "default", "dexAbstraction"]
)
def test_account_modes_do_not_double_count(client, wallet, account, mode):
    account.info.query_user_abstraction_state.return_value = mode
    r = result(client, wallet, "get_account", {"dex": "xyz"})
    assert r["accountMode"] == mode and r["balanceLocations"]["spot"]["balances"][0]["total"] == "9.903796"
    assert "perpMargin" not in r and "spot" not in r
    assert [p["dex"] for p in r["balanceLocations"]["perps"]] == ["", "xyz"]
    assert "availableBalance" not in r and "accountValue" not in r
    assert [c.kwargs["dex"] for c in account.info.user_state.call_args_list] == ["xyz", ""]
    assert r["balanceLocations"]["interpretation"] == (
        "separate"
        if mode == "disabled"
        else "shared"
        if mode in {"unifiedAccount", "portfolioMargin"}
        else "unverified"
    )


def test_unknown_mode_errors(client, wallet, account):
    account.info.query_user_abstraction_state.return_value = "futureMode"
    assert call(client, wallet, "hyperliquid_get_account", {})["isError"]


def test_optional_account_information(client, wallet, account):
    r = result(client, wallet, "get_account", {"include": ["fees", "subAccounts"]})
    assert r["subAccounts"] is None and r["fees"] == {"userCrossRate": "0.00045"}
    account.info.extra_agents.assert_not_called()
    account.info.user_state.assert_called_once()
    account.info.spot_user_state.assert_called_once()
    account.exchange_factory.assert_not_called()


@pytest.mark.parametrize(
    "mode,method",
    [("recent", "user_fills"), ("twap", "user_twap_slice_fills"), ("time", "user_fills_by_time")],
)
def test_fill_modes(client, wallet, account, mode, method):
    args = {"mode": mode}
    if mode == "time":
        args["startTime"] = 0
    r = result(client, wallet, "get_user_fills", args)
    assert r["completeHistory"] is False
    getattr(account.info, method).assert_called_once()


@pytest.mark.parametrize(
    "args",
    [
        {"mode": "recent", "startTime": 0},
        {"mode": "twap", "endTime": 1},
        {"mode": "time"},
        {"mode": "recent", "aggregateByTime": False},
    ],
)
def test_invalid_fill_filters_no_network(client, wallet, account, args):
    assert call(client, wallet, "hyperliquid_get_user_fills", args)["isError"]
    account.info.user_fills.assert_not_called()
    account.info.user_twap_slice_fills.assert_not_called()
    account.info.user_fills_by_time.assert_not_called()


def test_portfolio_vault_equities_opt_in(client, wallet, account):
    r = result(client, wallet, "get_portfolio", {})
    assert "vaultEquities" not in r
    account.info.user_vault_equities.assert_not_called()
    r = result(client, wallet, "get_portfolio", {"includeVaultEquities": True})
    assert r["vaultEquities"] == []


def test_ledger_time_range_rejected_before_sdk(client, wallet, account):
    assert call(client, wallet, "hyperliquid_get_ledger_updates", {"startTime": 2, "endTime": 1})["isError"]
    account.info_factory.assert_not_called()


def test_history_scope_and_empty_are_explicit(client, wallet, account):
    for name, args in [("get_orders", {"view": "history"}), ("get_ledger_updates", {"startTime": 0})]:
        r = result(client, wallet, name, args)
        assert r["data"] == [] and r["completeHistory"] is False


@pytest.mark.parametrize("which", ["balance", "portfolio", "orders", "ledger"])
def test_malformed_data_never_looks_empty(client, wallet, account, which):
    if which == "balance":
        account.info.spot_user_state.return_value = {
            "balances": [{"coin": "USDC", "token": 0, "total": "NaN", "hold": "0"}]
        }
        name, args = "get_account", {}
    elif which == "portfolio":
        account.info.portfolio.return_value = {"error": "bad"}
        name, args = "get_portfolio", {}
    elif which == "orders":
        account.info.historical_orders.return_value = [{}]
        name, args = "get_orders", {"view": "history"}
    else:
        account.info.user_non_funding_ledger_updates.return_value = [{}]
        name, args = "get_ledger_updates", {"startTime": 0}
    assert call(client, wallet, "hyperliquid_" + name, args)["isError"]
