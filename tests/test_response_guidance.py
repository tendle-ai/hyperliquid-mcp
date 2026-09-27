"""Agent-facing errors identify repairs without exposing input or claiming execution."""

import json

import pytest
from test_server import call


@pytest.mark.parametrize(
    "name,args,expected",
    [
        ("hyperliquid_set_account_mode", {}, "missing required field(s): mode"),
        ("hyperliquid_set_account_mode", {"mode": "invalid"}, "enum must match"),
        (
            "hyperliquid_transfer_collateral",
            {"source": "", "destination": "xyz", "tokenIndex": 0, "amount": 1},
            "at amount: type",
        ),
        (
            "hyperliquid_place_orders",
            {"orders": [{"marketId": "perp:ETH", "type": "market", "isBuy": True, "size": "secret-value"}]},
            "at orders.size: format",
        ),
        (
            "hyperliquid_place_orders",
            {
                "orders": [
                    {
                        "marketId": "perp:ETH",
                        "type": "market",
                        "isBuy": True,
                        "size": "1",
                        "price": "2000",
                    }
                ]
            },
            "invalid field combination",
        ),
    ],
)
def test_schema_error_is_actionable_before_execution(client, wallet, fake_sdk, name, args, expected):
    result = call(client, wallet, name, args)
    message = result["content"][0]["text"]
    assert result["isError"] and expected in message
    assert "No action submitted" in message and "secret-value" not in message
    fake_sdk.info_factory.assert_not_called()
    fake_sdk.exchange_factory.assert_not_called()


def test_unknown_properties_and_values_are_not_echoed(client, wallet, fake_sdk):
    secret = wallet.key.hex()
    result = call(client, wallet, "hyperliquid_set_account_mode", {"mode": "standard", secret: secret})
    assert result["isError"] and secret not in str(result)
    assert "unsupported fields" in str(result)
    fake_sdk.exchange_factory.assert_not_called()


def test_rejected_close_does_not_claim_submission(client, wallet, trading):
    trading.info.user_state.return_value["assetPositions"] = [
        {"position": {"coin": "ETH", "szi": "1", "leverage": {"type": "cross", "value": 10}}}
    ]
    trading.exchange.bulk_orders.return_value = {"status": "err", "response": "Example exchange rejection"}
    result = call(client, wallet, "hyperliquid_close_position", {"marketId": "perp:ETH"})
    assert result["isError"]
    data = json.loads(result["content"][0]["text"])
    assert data["data"]["response"] == "Example exchange rejection"
    assert "IOC submitted" not in data["note"]
    trading.exchange.bulk_orders.assert_called_once()
