import json

import pytest
from test_server import call


def read(client, wallet):
    r = call(client, wallet, "hyperliquid_get_account", {})
    assert not r.get("isError"), r
    return json.loads(r["content"][0]["text"])["balanceLocations"]["spot"]


def setup(fake_sdk, availability):
    fake_sdk.info.query_user_abstraction_state.return_value = "unifiedAccount"
    fake_sdk.info.spot_user_state.return_value = {
        "balances": [{"coin": "USDC", "token": 0, "total": "4831.464789", "hold": "4552.620271"}],
        "tokenToAvailableAfterMaintenance": availability,
    }


def test_user_snapshot_distinguishes_held_total_and_available(client, wallet, fake_sdk):
    setup(fake_sdk, [[0, "278.844518"]])
    s = read(client, wallet)
    assert s["balances"] == [
        {
            "coin": "USDC",
            "token": 0,
            "availableAfterMaintenance": "278.844518",
            "held": "4552.620271",
            "total": "4831.464789",
            "unheld": "278.844518",
        }
    ]
    assert "tokenToAvailableAfterMaintenance" not in s
    fake_sdk.info.spot_user_state.assert_called_once()
    fake_sdk.exchange_factory.assert_not_called()


@pytest.mark.parametrize("value", ["0", "100", "-1.25"])
def test_exchange_availability_preserved_not_recomputed(client, wallet, fake_sdk, value):
    setup(fake_sdk, [[0, value]])
    assert read(client, wallet)["balances"][0]["availableAfterMaintenance"] == value


def test_missing_availability_is_null_not_total_or_unheld(client, wallet, fake_sdk):
    setup(fake_sdk, [])
    s = read(client, wallet)["balances"][0]
    assert s["availableAfterMaintenance"] is None and s["unheld"] == "278.844518"
    del fake_sdk.info.spot_user_state.return_value["tokenToAvailableAfterMaintenance"]
    assert read(client, wallet)["balances"][0]["availableAfterMaintenance"] is None


@pytest.mark.parametrize(
    "bad", [None, {}, [[0, "NaN"]], [[0, "1"], [0, "2"]], [[True, "1"]], [[0]], [[0, 1]]]
)
def test_invalid_maintenance_data_fails_closed(client, wallet, fake_sdk, bad):
    setup(fake_sdk, bad)
    assert call(client, wallet, "hyperliquid_get_account", {})["isError"]


def test_tokens_are_joined_by_id_and_never_summed(client, wallet, fake_sdk):
    setup(fake_sdk, [[360, "4"], [0, "278.844518"]])
    fake_sdk.info.spot_user_state.return_value["balances"].append(
        {"coin": "USDH", "token": 360, "total": "9", "hold": "3"}
    )
    rows = read(client, wallet)["balances"]
    assert rows[0]["availableAfterMaintenance"] == "278.844518"
    assert rows[1]["availableAfterMaintenance"] == "4" and rows[1]["unheld"] == "6"
