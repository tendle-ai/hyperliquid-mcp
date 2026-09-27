import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from eth_account import Account
from eth_account._utils.legacy_transactions import Transaction
from eth_utils import keccak
from starlette.testclient import TestClient
from test_server import BASE, call, rpc

from hosted_hyperliquid import bridge, server


@pytest.fixture
def chain_rpc(monkeypatch):
    state = SimpleNamespace(
        chain=421614, usdc=20_000_000, eth=10**17, receipt=None, failure=None, calls=[], raw=None, hash=None
    )

    def post(self, url, *, json, timeout, allow_redirects):
        method, params = json["method"], json["params"]
        state.calls.append((method, params))
        assert timeout == 10 and allow_redirects is False
        if method == state.failure:
            raise RuntimeError("RPC secret that must be redacted")
        if method == "eth_chainId":
            result = hex(state.chain)
        elif method == "eth_call":
            result = hex(state.usdc) if params[0]["data"].startswith("0x70a08231") else "0x1"
        elif method == "eth_estimateGas":
            result = hex(50_000)
        elif method == "eth_gasPrice":
            result = hex(20_000_000)
        elif method == "eth_getBalance":
            result = hex(state.eth)
        elif method == "eth_getTransactionCount":
            assert params[1] == "pending"
            result = "0x7"
        elif method == "eth_sendRawTransaction":
            state.raw = bytes.fromhex(params[0][2:])
            state.hash = "0x" + keccak(state.raw).hex()
            result = state.hash
        elif method == "eth_getTransactionReceipt":
            result = (
                None
                if state.receipt is None
                else {"transactionHash": state.hash, "status": hex(state.receipt), "blockNumber": "0x123"}
            )
        else:
            raise AssertionError(method)
        response = Mock()
        response.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": result}
        return response

    monkeypatch.delenv("ARBITRUM_RPC_URL", raising=False)
    monkeypatch.setattr(bridge.requests.Session, "post", post)
    return state


@pytest.mark.parametrize("testnet,chain", [(True, 421614), (False, 42161)])
def test_deposit_signs_expected_transfer(wallet, chain_rpc, testnet, chain, caplog):
    chain_rpc.chain = chain
    with TestClient(server.create_app(public_url=BASE, testnet=testnet), base_url=BASE) as c:
        result = call(c, wallet, "hyperliquid_deposit", {"amount": "12.345678"})
    assert not result.get("isError", False)
    data = json.loads(result["content"][0]["text"])["data"]
    assert data["status"] == "submitted" and not data["hyperliquidCreditConfirmed"]
    assert data["transactionHash"] == chain_rpc.hash
    assert Account.recover_transaction(chain_rpc.raw) == wallet.address
    tx = Transaction.from_bytes(chain_rpc.raw)
    assert (tx.v - 35) // 2 == chain
    assert tx.nonce == 7 and tx.value == 0 and tx.gas == 60_000 and tx.gasPrice == 20_000_000
    expected_token = (
        "af88d065e77c8cc2239327c5edb3a432268e5831"
        if not testnet
        else "1baabb04529d43a73232b713c0fe471f7c7334d5"
    )
    expected_bridge = (
        "2df1c51e09aecf9cacb7bc98cb1742757f163df7"
        if not testnet
        else "08cfc1b6b2dcf36a1480b99353a354aa8ac56f89"
    )
    assert tx.to.hex() == expected_token
    assert tx.data[:4] == keccak(text="transfer(address,uint256)")[:4]
    assert len(tx.data) == 68 and tx.data[4:16] == bytes(12)
    assert tx.data[16:36].hex() == expected_bridge
    assert int.from_bytes(tx.data[36:], "big") == 12_345_678
    assert sum(m == "eth_sendRawTransaction" for m, _ in chain_rpc.calls) == 1
    assert wallet.key.hex() not in json.dumps(result) + caplog.text


@pytest.mark.parametrize(
    "amount", ["4.999999", "0", "-5", "NaN", "Infinity", "1e6", "5.0000001", " 5", "5\n", 5, "9" * 78]
)
def test_invalid_amount_never_contacts_rpc(client, wallet, chain_rpc, amount):
    assert call(client, wallet, "hyperliquid_deposit", {"amount": amount})["isError"]
    assert not chain_rpc.calls


@pytest.mark.parametrize("header", ["X-Hyperliquid-Account-Address", "X-Hyperliquid-Vault-Address"])
def test_override_rejected(client, wallet, chain_rpc, header):
    assert call(client, wallet, "hyperliquid_deposit", {"amount": "5"}, {header: Account.create().address})[
        "isError"
    ]
    assert not chain_rpc.calls


@pytest.mark.parametrize(
    "field,value", [("chain", 1), ("usdc", 4_999_999), ("eth", 0), ("failure", "eth_estimateGas")]
)
def test_preflight_failure_never_broadcasts(client, wallet, chain_rpc, field, value):
    setattr(chain_rpc, field, value)
    result = call(client, wallet, "hyperliquid_deposit", {"amount": "5"})
    assert result["isError"]
    assert not any(m == "eth_sendRawTransaction" for m, _ in chain_rpc.calls)
    assert "RPC secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "receipt,status,error", [(None, "submitted", False), (1, "confirmed", False), (0, "reverted", True)]
)
def test_receipt_status(client, wallet, chain_rpc, receipt, status, error):
    chain_rpc.receipt = receipt
    result = call(client, wallet, "hyperliquid_deposit", {"amount": "5"})
    assert bool(result.get("isError")) == error
    data = json.loads(result["content"][0]["text"])["data"]
    assert data["status"] == status
    assert data["hyperliquidCreditConfirmed"] is False


@pytest.mark.parametrize(
    "method,status,error",
    [("eth_sendRawTransaction", "unknown", True), ("eth_getTransactionReceipt", "submitted", False)],
)
def test_ambiguous_outcomes_keep_hash_without_retry(client, wallet, chain_rpc, method, status, error):
    chain_rpc.failure = method
    result = call(client, wallet, "hyperliquid_deposit", {"amount": "5"})
    assert bool(result.get("isError")) == error
    data = json.loads(result["content"][0]["text"])["data"]
    assert data["status"] == status and len(data["transactionHash"]) == 66
    assert sum(m == "eth_sendRawTransaction" for m, _ in chain_rpc.calls) == 1
    assert "RPC secret" not in json.dumps(result)


def test_discovery_auth_and_removed_preview(client, wallet, chain_rpc):
    assert call(client, None, "hyperliquid_deposit", {"amount": "5"})["isError"]
    assert chain_rpc.calls == []
    assert call(client, wallet, "hyperliquid_prepare_deposit", {"amount": "5"})["isError"]
    assert call(client, wallet, "hyperliquid_deposit", {"amount": "5", "recipient": wallet.address})[
        "isError"
    ]
    listing = rpc(client, wallet).json()["result"]["tools"]
    names = {t["name"] for t in listing}
    assert len(names) == 30
    assert not names & {
        "hyperliquid_prepare_deposit",
        "hyperliquid_place_twap_order",
        "hyperliquid_cancel_twap_order",
        "hyperliquid_get_recent_trades",
        "hyperliquid_vault_performance",
    }
    tool = next(t for t in listing if t["name"] == "hyperliquid_deposit")
    assert tool["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    assert not chain_rpc.calls
