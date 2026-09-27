import json
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient
from test_server import BASE, call

from hosted_hyperliquid import server, vaults


@pytest.fixture
def directory(monkeypatch):
    entries = [
        {
            "apr": 0.1,
            "summary": {
                "name": name,
                "vaultAddress": address,
                "leader": "0x" + "1" * 40,
                "tvl": tvl,
                "isClosed": closed,
            },
        }
        for name, address, tvl, closed in [
            ("Small", "0xaaa" + "0" * 37, "9", False),
            ("Large", "0xbbb" + "0" * 37, "100", False),
            ("Closed", "0xccc" + "0" * 37, "1000", True),
        ]
    ]
    response = Mock()
    response.json.return_value = entries
    get = Mock(return_value=response)
    monkeypatch.setattr(vaults.requests.Session, "get", get)
    return get, response


def test_pagination_sort_and_filter(client, wallet, directory):
    result = call(client, wallet, "hyperliquid_list_vaults", {"limit": 1})
    data = json.loads(result["content"][0]["text"])["data"]
    assert data["total"] == 2 and data["nextOffset"] == 1
    assert data["vaults"][0]["name"] == "Large"
    result = call(client, wallet, "hyperliquid_list_vaults", {"offset": 1})
    data = json.loads(result["content"][0]["text"])["data"]
    assert data["nextOffset"] is None and data["vaults"][0]["name"] == "Small"
    result = call(client, wallet, "hyperliquid_list_vaults", {"includeClosed": True})
    data = json.loads(result["content"][0]["text"])["data"]
    assert data["total"] == 3 and data["vaults"][0]["isClosed"]


@pytest.mark.parametrize("search,expected", [("LARGE", 1), ("0xaaa", 1), ("0x111", 2), ("missing", 0)])
def test_search(client, wallet, directory, search, expected):
    result = call(client, wallet, "hyperliquid_list_vaults", {"search": search})
    assert json.loads(result["content"][0]["text"])["data"]["total"] == expected


@pytest.mark.parametrize("testnet,network", [(True, "Testnet"), (False, "Mainnet")])
def test_network_and_no_credential_forwarding(wallet, directory, testnet, network):
    with TestClient(server.create_app(public_url=BASE, testnet=testnet), base_url=BASE) as client:
        result = call(client, wallet, "hyperliquid_list_vaults")
    assert not result.get("isError", False)
    directory[0].assert_called_once_with(
        f"https://stats-data.hyperliquid.xyz/{network}/vaults", timeout=10.0, allow_redirects=False
    )
    assert wallet.key.hex() not in json.dumps(result)


@pytest.mark.parametrize(
    "args", [{"limit": 101}, {"offset": -1}, {"limit": 1.5}, {"includeClosed": "yes"}, {"other": 1}]
)
def test_schema(client, wallet, directory, args):
    assert call(client, wallet, "hyperliquid_list_vaults", args)["isError"]
    directory[0].assert_not_called()


def test_auth_and_upstream_failure(client, wallet, directory):
    assert call(client, None, "hyperliquid_list_vaults")["isError"]
    directory[0].assert_not_called()
    directory[1].json.return_value = {"unexpected": "format"}
    assert call(client, wallet, "hyperliquid_list_vaults")["isError"]
    directory[0].side_effect = RuntimeError(wallet.key.hex())
    result = call(client, wallet, "hyperliquid_list_vaults")
    assert result["isError"] and wallet.key.hex() not in json.dumps(result)


def test_directory_empty_and_offset_past_end(client, wallet, directory):
    for args in ({"offset": 9999}, {"search": "missing"}):
        result = call(client, wallet, "hyperliquid_list_vaults", args)
        data = json.loads(result["content"][0]["text"])["data"]
        assert data["vaults"] == [] and data["nextOffset"] is None
    directory[1].json.return_value = []
    result = call(client, wallet, "hyperliquid_list_vaults")
    assert json.loads(result["content"][0]["text"])["data"]["total"] == 0


@pytest.mark.parametrize(
    "field,bad",
    [
        ("tvl", "NaN"),
        ("tvl", None),
        ("isClosed", "false"),
        ("vaultAddress", "bad"),
        ("leader", None),
        ("name", None),
    ],
)
def test_directory_malformed_summary(client, wallet, directory, field, bad):
    directory[1].json.return_value[0]["summary"][field] = bad
    assert call(client, wallet, "hyperliquid_list_vaults")["isError"]


def test_directory_large_tvl_exact_sort(client, wallet, directory):
    entries = directory[1].json.return_value
    entries[0]["summary"]["tvl"] = "100000000000000000000000000000.000001"
    entries[1]["summary"]["tvl"] = "100000000000000000000000000000.000002"
    result = call(client, wallet, "hyperliquid_list_vaults")
    data = json.loads(result["content"][0]["text"])["data"]
    assert [v["name"] for v in data["vaults"]] == ["Large", "Small"]


@pytest.mark.parametrize("apr", [float("nan"), float("inf"), True, "0.1"])
def test_directory_bad_apr(client, wallet, directory, apr):
    directory[1].json.return_value[0]["apr"] = apr
    assert call(client, wallet, "hyperliquid_list_vaults")["isError"]
