"""Agent discovery → explicit selection → correct signer, without live writes."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from eth_account import Account
from test_server import SEED_VECTOR, call

from hosted_hyperliquid.auth import current_wallet


def invoke(client, name, args=None, phrase=SEED_VECTOR, headers=None):
    return call(
        client, None, "hyperliquid_" + name, args, {"Authorization": "Bearer " + phrase, **(headers or {})}
    )


def data(result):
    assert not result.get("isError", False), result
    return json.loads(result["content"][0]["text"])


def selection(index):
    return {
        "index": index,
        "address": Account.from_mnemonic(SEED_VECTOR, account_path=f"m/44'/60'/0'/0/{index}").address,
    }


def test_discovery_pagination_is_local_and_secret_free(client, fake_sdk, caplog):
    first = data(invoke(client, "list_wallet_accounts"))
    second = data(invoke(client, "list_wallet_accounts", {"startIndex": first["nextIndex"], "limit": 2}))
    assert first["credentialType"] == "seedPhrase"
    assert first["accounts"] == [selection(i) for i in range(10)]
    assert second["accounts"] == [selection(10), selection(11)]
    assert second["nextIndex"] == 12
    assert SEED_VECTOR not in json.dumps(first) + json.dumps(second) + caplog.text
    assert "privateKey" not in json.dumps(first)
    fake_sdk.info_factory.assert_not_called()
    fake_sdk.exchange_factory.assert_not_called()


def test_agent_selects_nonzero_account_and_signs_with_it(client, trading):
    chosen = data(invoke(client, "list_wallet_accounts", {"startIndex": 3, "limit": 1}))["accounts"][0]
    assert data(invoke(client, "get_account", {"walletAccount": chosen}))["userAddress"] == chosen["address"]
    data(
        invoke(
            client,
            "place_orders",
            {
                "walletAccount": chosen,
                "orders": [
                    {"marketId": "perp:ETH", "size": "0.1", "price": "2000", "isBuy": True, "type": "limit"}
                ],
            },
        )
    )
    signer = trading.exchange_factory.call_args.args[0]
    assert signer.address == chosen["address"]
    assert signer.address != selection(0)["address"]
    trading.exchange.bulk_orders.assert_called_once()
    # Selection must not persist across calls, even after a successful write.
    assert invoke(client, "get_account")["isError"]
    with pytest.raises(LookupError):
        current_wallet.get()


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_account", {}),
        ("deposit", {"amount": "5"}),
        ("withdraw", {"amount": "2"}),
        ("set_account_mode", {"mode": "unified"}),
        (
            "place_orders",
            {"orders": [{"marketId": "perp:ETH", "type": "market", "isBuy": True, "size": "1"}]},
        ),
    ],
)
@pytest.mark.parametrize("choice", [None, {"index": 1, "address": selection(0)["address"]}])
def test_missing_or_mismatched_selection_never_reaches_sdk(client, fake_sdk, name, args, choice):
    if choice is not None:
        args = {**args, "walletAccount": choice}
    result = invoke(client, name, args)
    assert result["isError"]
    assert "No action submitted" in result["content"][0]["text"]
    fake_sdk.info_factory.assert_not_called()
    fake_sdk.exchange_factory.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        {"startIndex": -1},
        {"startIndex": 2**31},
        {"startIndex": True},
        {"limit": 0},
        {"limit": 11},
        {"limit": 1.5},
        {"seedPhrase": SEED_VECTOR},
    ],
)
def test_discovery_rejects_invalid_ranges_and_secret_arguments(client, args):
    result = invoke(client, "list_wallet_accounts", args)
    assert result["isError"]
    assert SEED_VECTOR not in json.dumps(result)


def test_end_of_derivation_range_and_integer_json_numbers(client):
    result = data(invoke(client, "list_wallet_accounts", {"startIndex": 2**31 - 1, "limit": 10}))
    assert len(result["accounts"]) == 1 and result["nextIndex"] is None
    result = data(invoke(client, "list_wallet_accounts", {"startIndex": 1.0, "limit": 1.0}))
    assert result["accounts"] == [selection(1)]


@pytest.mark.parametrize(
    "choice",
    [
        {"index": True, "address": selection(0)["address"]},
        {"index": -1, "address": selection(0)["address"]},
        {"index": 2**31, "address": selection(0)["address"]},
        {"index": 1.5, "address": selection(0)["address"]},
        {"index": 0},
        {"address": selection(0)["address"]},
    ],
)
def test_bad_selector_schema(client, fake_sdk, choice):
    assert invoke(client, "get_account", {"walletAccount": choice})["isError"]
    fake_sdk.info_factory.assert_not_called()


def test_private_key_needs_no_selector_and_rejects_one(client, wallet, fake_sdk):
    result = data(call(client, wallet, "hyperliquid_list_wallet_accounts"))
    assert result["accounts"] == [{"address": wallet.address}]
    assert result["nextIndex"] is None
    assert call(client, wallet, "hyperliquid_get_account", {"walletAccount": selection(0)})["isError"]
    assert data(call(client, wallet, "hyperliquid_get_account"))["userAddress"] == wallet.address


def test_changed_phrase_rejects_stale_choice(client, fake_sdk):
    _, other_phrase = Account.create_with_mnemonic()
    assert invoke(client, "get_account", {"walletAccount": selection(0)}, phrase=other_phrase)["isError"]
    fake_sdk.info_factory.assert_not_called()


def test_read_subject_and_owner_override_are_not_signer_selection(client, fake_sdk):
    chosen, subject = selection(2), selection(4)["address"]
    assert (
        data(invoke(client, "get_account", {"walletAccount": chosen, "userAddress": subject}))["userAddress"]
        == subject
    )
    assert invoke(client, "get_account", {"userAddress": subject})["isError"]
    result = invoke(
        client,
        "withdraw",
        {"walletAccount": chosen, "amount": "2"},
        headers={"X-Hyperliquid-Account-Address": subject},
    )
    assert result["isError"]
    fake_sdk.exchange_factory.assert_not_called()


def test_concurrent_selections_do_not_leak_between_requests(client, fake_sdk):
    def read(index):
        chosen = selection(index)
        assert (
            data(invoke(client, "get_account", {"walletAccount": chosen}))["userAddress"] == chosen["address"]
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(read, range(4)))


def test_lowercase_address_and_integral_float_index(client, fake_sdk):
    chosen = selection(1)
    chosen["index"] = 1.0
    chosen["address"] = chosen["address"].lower()
    assert (
        data(invoke(client, "get_account", {"walletAccount": chosen}))["userAddress"].lower()
        == chosen["address"]
    )
