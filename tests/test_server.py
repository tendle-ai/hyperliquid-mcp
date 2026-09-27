import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from eth_account import Account

from hosted_hyperliquid import server, tools
from hosted_hyperliquid.auth import current_wallet

BASE = "http://127.0.0.1:8001"


def rpc(client, wallet, method="tools/list", params=None, extra_headers=None):
    params = dict(params or {})
    params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    }
    if wallet:
        headers["Authorization"] = "Bearer " + wallet.key.hex()
    if "name" in params:
        headers["Mcp-Name"] = params["name"]
    headers.update(extra_headers or {})
    return client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, headers=headers
    )


def call(client, wallet, name, args=None, headers=None):
    response = rpc(client, wallet, "tools/call", {"name": name, "arguments": args or {}}, headers)
    assert response.status_code == 200, response.text
    return response.json()["result"]


@pytest.mark.parametrize(
    "credential",
    [
        "",
        "Basic abc",
        "Bearer not-a-key",
        "Bearer " + "0" * 64,
        "Bearer " + "f" * 64,
        "Bearer " + "1" * 62,
    ],
)
def test_rejects_bad_credentials(client, credential):
    headers = {"Mcp-Session-Id": "cannot-bypass-auth"}
    if credential is not None:
        headers["Authorization"] = credential
    response = rpc(client, None, extra_headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    if credential:
        assert credential not in response.text


def test_duplicate_auth_rejected(client, wallet):
    value = "Bearer " + wallet.key.hex()
    response = client.post("/mcp", headers=[("Authorization", value), ("Authorization", value)], json={})
    assert response.status_code == 401


def test_health_is_public(client):
    assert client.get("/healthz").json() == {"status": "ok", "network": "testnet"}


@pytest.mark.parametrize("authenticated", [True, False])
def test_standard_mcp_initialization_and_calls(client, wallet, authenticated, fake_sdk):
    headers = {
        "Authorization": "Bearer " + wallet.key.hex(),
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    if not authenticated:
        del headers["Authorization"]
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["protocolVersion"] == "2025-11-25"
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "hyperliquid_get_account" if authenticated else "hyperliquid_generate_wallet",
                "arguments": {},
            },
        },
    )
    assert response.status_code == 200, response.text
    assert not response.json()["result"].get("isError", False)


def test_account_headers_validated(client, wallet):
    assert rpc(client, wallet, extra_headers={"X-Hyperliquid-Account-Address": "bad"}).status_code == 400


def test_concurrent_wallets_are_isolated(client, monkeypatch):
    wallets = [Account.create(), Account.create()]
    targets = [Account.create().address, Account.create().address]
    vaults = [Account.create().address, Account.create().address]
    barrier = threading.Barrier(2)

    def execute(self, name, arguments):
        barrier.wait(timeout=5)
        return {"signer": self.wallet.address, "account": self.account_address, "vault": self.vault_address}

    monkeypatch.setattr(tools.HyperliquidTools, "execute", execute)

    def invoke(i):
        result = call(
            client,
            wallets[i],
            "hyperliquid_get_account",
            headers={"X-Hyperliquid-Account-Address": targets[i], "X-Hyperliquid-Vault-Address": vaults[i]},
        )
        return json.loads(result["content"][0]["text"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, [0, 1]))
    for i, result in enumerate(results):
        assert result == {"signer": wallets[i].address, "account": targets[i], "vault": vaults[i]}


def test_exceptions_do_not_leak_credentials(client, wallet, monkeypatch, caplog):
    def fail(self, name, arguments):
        raise RuntimeError(self.wallet.key.hex())

    monkeypatch.setattr(tools.HyperliquidTools, "execute", fail)
    result = call(client, wallet, "hyperliquid_get_account")
    assert result["isError"]
    assert wallet.key.hex() not in json.dumps(result) + caplog.text


@pytest.mark.parametrize(
    "name,args",
    [
        ("hyperliquid_place_twap_order", {"coin": "ETH", "isBuy": True, "size": "1", "minutes": 2}),
        ("hyperliquid_cancel_twap_order", {"twapId": 1}),
        ("hyperliquid_get_recent_trades", {"coin": "ETH"}),
        ("hyperliquid_vault_performance", {"vaultAddress": "0x" + "1" * 40, "startTime": 0}),
    ],
)
def test_removed_tools_are_unknown(client, wallet, name, args):
    result = call(client, wallet, name, args)
    assert result["isError"]
    assert "Unknown tool" in result["content"][0]["text"]


def test_origin_and_host_validation(client, wallet):
    assert rpc(client, wallet, extra_headers={"Origin": "https://untrusted.example"}).status_code == 403
    assert rpc(client, wallet, extra_headers={"Host": "untrusted.example"}).status_code == 421


def test_public_plain_http_configuration_rejected():
    with pytest.raises(ValueError, match="HTTPS"):
        server.create_app(public_url="http://mcp.example.com")


def test_generate_wallet_returns_valid_distinct_keys_without_network(client, wallet, caplog):
    generated = []
    for _ in range(3):
        result = call(client, wallet, "hyperliquid_generate_wallet")
        assert not result.get("isError", False)
        data = json.loads(result["content"][0]["text"])["data"]
        key = data["privateKey"]
        assert len(key) == 66 and key.startswith("0x")
        created = Account.from_key(key)
        assert created.address == data["address"]
        assert created.address != wallet.address
        assert key not in caplog.text
        assert key[2:] not in caplog.text
        generated.append(created)
    assert len({item.address for item in generated}) == 3
    # The generated credential can authenticate a subsequent MCP request.
    assert rpc(client, generated[0]).status_code == 200


@pytest.mark.parametrize("name", sorted(set(server.TOOLS) - server.PUBLIC_TOOLS))
def test_public_bootstrap_does_not_authorize_other_tools(client, monkeypatch, name):
    execute = Mock()
    monkeypatch.setattr(server, "execute_tool", execute)
    result = call(client, None, name, {})
    assert result["isError"]
    assert "Authentication required" in result["content"][0]["text"]
    execute.assert_not_called()


def test_public_wallet_bootstrap(client, fake_sdk):
    listing = rpc(client, None)
    assert listing.status_code == 200
    assert len(listing.json()["result"]["tools"]) == 30
    response = rpc(client, None, "tools/call", {"name": "hyperliquid_generate_wallet", "arguments": {}})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = json.loads(response.json()["result"]["content"][0]["text"])["data"]
    generated = Account.from_key(data["privateKey"])
    assert generated.address == data["address"]
    assert not call(client, generated, "hyperliquid_get_account").get("isError", False)
    assert call(client, None, "hyperliquid_get_account")["isError"]
    with pytest.raises(LookupError):
        current_wallet.get()


def test_wallet_generation_rejects_extra_parameters(client, wallet):
    result = call(client, wallet, "hyperliquid_generate_wallet", {"seed": "user-supplied-entropy"})
    assert result["isError"]


def test_wallet_generation_is_marked_non_idempotent(client, wallet):
    listing = rpc(client, wallet).json()["result"]["tools"]
    generated_tool = next(tool for tool in listing if tool["name"] == "hyperliquid_generate_wallet")
    assert generated_tool["annotations"]["idempotentHint"] is False
    assert generated_tool["annotations"]["openWorldHint"] is False


def test_vault_details_not_found(client, wallet, fake_sdk):
    fake_sdk.info.post.return_value = None
    result = call(client, wallet, "hyperliquid_vault_details", {"vaultAddress": "0x" + "0" * 40})
    assert result["isError"]
    assert "Vault not found" in result["content"][0]["text"]
    fake_sdk.info.post.assert_called_once_with(
        "/info", {"type": "vaultDetails", "vaultAddress": "0x" + "0" * 40}
    )


@pytest.mark.parametrize("address", ["invalid", "0x123", "0x" + "g" * 40, "0x" + "1" * 40 + "\n"])
def test_vault_details_bad_address_never_contacts_upstream(client, wallet, fake_sdk, address):
    result = call(client, wallet, "hyperliquid_vault_details", {"vaultAddress": address})
    assert result["isError"]
    fake_sdk.info_factory.assert_not_called()


def test_vault_details_preserves_deep_fields(client, wallet, fake_sdk):
    address = "0x" + "1" * 40
    details = {
        "name": "Example",
        "vaultAddress": address,
        "description": "Strategy",
        "portfolio": [["day", {"pnlHistory": [[1, "3"]]}]],
        "followers": [{"user": wallet.address, "vaultEquity": "20"}],
        "leaderCommission": 0.1,
        "allowDeposits": True,
    }
    fake_sdk.info.post.return_value = details
    result = call(client, wallet, "hyperliquid_vault_details", {"vaultAddress": address})
    assert not result.get("isError", False)
    assert json.loads(result["content"][0]["text"])["data"] == details


def test_wallet_generation_concurrent_isolation(client, wallet, caplog):
    from eth_account.messages import encode_defunct

    def generate(_):
        response = rpc(client, wallet, "tools/call", {"name": "hyperliquid_generate_wallet", "arguments": {}})
        assert response.headers["cache-control"] == "no-store"
        assert "mcp-session-id" not in response.headers
        result = response.json()["result"]
        assert not result.get("isError", False)
        data = json.loads(result["content"][0]["text"])["data"]
        account = Account.from_key(data["privateKey"])
        assert account.address == data["address"]
        assert Account.from_mnemonic(data["seedPhrase"]).key == account.key
        assert data["seedPhrase"] not in caplog.text
        assert (
            rpc(client, None, extra_headers={"Authorization": "Bearer " + data["seedPhrase"]}).status_code
            == 200
        )
        message = encode_defunct(text="Wallet test only; no transaction authorization")
        assert (
            Account.recover_message(message, signature=account.sign_message(message).signature)
            == account.address
        )
        assert rpc(client, account).status_code == 200
        assert data["privateKey"][2:] not in caplog.text
        return account.address

    with ThreadPoolExecutor(max_workers=6) as pool:
        addresses = list(pool.map(generate, range(12)))
    assert len(set(addresses)) == 12
    assert wallet.address not in addresses


def test_wallet_generation_entropy_failure_is_redacted_and_recovers(client, wallet, monkeypatch, caplog):
    original = Account.create_with_mnemonic

    def fail(*args, **kwargs):
        raise OSError("Random source failed " + wallet.key.hex())

    monkeypatch.setattr(Account, "create_with_mnemonic", fail)
    result = call(client, wallet, "hyperliquid_generate_wallet")
    assert result["isError"]
    assert "Wallet generation failed" in result["content"][0]["text"]
    assert wallet.key.hex() not in json.dumps(result) + caplog.text
    assert "privateKey" not in json.dumps(result)
    monkeypatch.setattr(Account, "create_with_mnemonic", original)
    assert not call(client, wallet, "hyperliquid_generate_wallet").get("isError", False)


@pytest.mark.parametrize(
    "authorization", ["Bearer " + "0" * 64, "Bearer " + "f" * 64, "Bearer invalid", "Basic invalid"]
)
def test_wallet_generation_invalid_auth_never_generates(client, monkeypatch, authorization):
    create = Mock(side_effect=AssertionError("Must not generate"))
    monkeypatch.setattr(Account, "create_with_mnemonic", create)
    response = rpc(
        client,
        None,
        "tools/call",
        {"name": "hyperliquid_generate_wallet", "arguments": {}},
        {"Authorization": authorization},
    )
    assert response.status_code == 401
    create.assert_not_called()


def test_wallet_generation_extra_args_never_generate(client, wallet, monkeypatch):
    create = Mock(side_effect=AssertionError("Must not generate"))
    monkeypatch.setattr(Account, "create_with_mnemonic", create)
    assert call(client, wallet, "hyperliquid_generate_wallet", {"privateKey": "not allowed"})["isError"]
    create.assert_not_called()


@pytest.mark.parametrize("explicit", [True, False])
def test_withdraw_submits_exact_amount_and_destination(client, wallet, fake_sdk, explicit):
    destination = Account.create().address if explicit else wallet.address
    fake_sdk.exchange.withdraw_from_bridge.return_value = {"status": "ok", "response": {"type": "default"}}
    args = {"amount": "12345678901234.123456"}
    if explicit:
        args["destination"] = destination
    result = call(client, wallet, "hyperliquid_withdraw", args)
    assert not result.get("isError", False)
    fake_sdk.exchange.withdraw_from_bridge.assert_called_once_with(args["amount"], destination)
    data = json.loads(result["content"][0]["text"])["data"]
    assert data["status"] == "submitted" and data["arrivalConfirmed"] is False
    fake_sdk.exchange.session.close.assert_called_once()


@pytest.mark.parametrize(
    "args",
    [
        {"amount": "0"},
        {"amount": "-1"},
        {"amount": "NaN"},
        {"amount": "Infinity"},
        {"amount": "1e3"},
        {"amount": "2.0000001"},
        {"amount": 2},
        {"amount": "2", "destination": "0x" + "0" * 40},
        {"amount": "2", "destination": "garbage"},
        {"amount": "2", "destination": "0x52908400098527886E0F7030069857D2E4169Ee7"},
    ],
)
def test_withdraw_invalid_inputs_never_construct_exchange(client, wallet, fake_sdk, args):
    assert call(client, wallet, "hyperliquid_withdraw", args)["isError"]
    fake_sdk.exchange_factory.assert_not_called()


@pytest.mark.parametrize("header", ["X-Hyperliquid-Account-Address", "X-Hyperliquid-Vault-Address"])
def test_withdraw_override_rejected(client, wallet, fake_sdk, header):
    assert call(client, wallet, "hyperliquid_withdraw", {"amount": "2"}, {header: Account.create().address})[
        "isError"
    ]
    fake_sdk.exchange_factory.assert_not_called()


@pytest.mark.parametrize(
    "response,status",
    [
        ({"status": "err", "response": "Insufficient withdrawable balance"}, "rejected"),
        ({"status": "unexpected"}, "unknown"),
        ({"status": "ok", "response": None}, "unknown"),
        (None, "unknown"),
    ],
)
def test_withdraw_failure_responses(client, wallet, fake_sdk, response, status):
    fake_sdk.exchange.withdraw_from_bridge.return_value = response
    result = call(client, wallet, "hyperliquid_withdraw", {"amount": "2"})
    assert result["isError"]
    assert json.loads(result["content"][0]["text"])["data"]["status"] == status
    fake_sdk.exchange.withdraw_from_bridge.assert_called_once()


def test_withdraw_timeout_no_retry_or_secret_leak(client, wallet, fake_sdk, caplog):
    fake_sdk.exchange.withdraw_from_bridge.side_effect = TimeoutError(wallet.key.hex())
    result = call(client, wallet, "hyperliquid_withdraw", {"amount": "2"})
    assert result["isError"]
    assert json.loads(result["content"][0]["text"])["data"]["status"] == "unknown"
    assert wallet.key.hex() not in json.dumps(result) + caplog.text
    fake_sdk.exchange.withdraw_from_bridge.assert_called_once()


@pytest.mark.parametrize("mainnet", [True, False])
def test_real_sdk_withdraw_signature_offline(wallet, mainnet):
    from hyperliquid.exchange import Exchange
    from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL
    from hyperliquid.utils.signing import WITHDRAW_SIGN_TYPES, recover_user_from_user_signed_action

    exchange = object.__new__(Exchange)
    exchange.wallet = wallet
    exchange.base_url = MAINNET_API_URL if mainnet else TESTNET_API_URL
    exchange._post_action = Mock(return_value={"status": "ok", "response": {"type": "default"}})
    amount = "12345678901234.123456"
    exchange.withdraw_from_bridge(amount, wallet.address)
    action, signature, nonce = exchange._post_action.call_args.args
    assert action["amount"] == amount and action["destination"] == wallet.address
    assert action["type"] == "withdraw3" and action["time"] == nonce
    assert action["hyperliquidChain"] == ("Mainnet" if mainnet else "Testnet")
    recovered = recover_user_from_user_signed_action(
        action, signature, WITHDRAW_SIGN_TYPES, "HyperliquidTransaction:Withdraw", mainnet
    )
    assert recovered == wallet.address


def test_withdraw_auth_and_annotations(client, wallet, fake_sdk):
    assert call(client, None, "hyperliquid_withdraw", {"amount": "2"})["isError"]
    fake_sdk.exchange_factory.assert_not_called()
    listing = rpc(client, wallet).json()["result"]["tools"]
    tool = next(t for t in listing if t["name"] == "hyperliquid_withdraw")
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["idempotentHint"] is False
    assert "hyperliquid_withdraw" in tools.WRITE_TOOLS


@pytest.mark.parametrize("arguments", [[], [1], "", "bad", False, 0])
def test_wallet_generation_wrong_argument_types_never_generate(client, wallet, monkeypatch, arguments):
    create = Mock(side_effect=AssertionError("Invalid request must not generate"))
    monkeypatch.setattr(Account, "create_with_mnemonic", create)
    response = rpc(
        client, wallet, "tools/call", {"name": "hyperliquid_generate_wallet", "arguments": arguments}
    )
    body = response.json()
    assert response.status_code >= 400 or "error" in body or body.get("result", {}).get("isError")
    create.assert_not_called()


def test_wallet_generation_oversized_request_never_generates(client, wallet, monkeypatch):
    create = Mock(side_effect=AssertionError("Oversized request must not generate"))
    monkeypatch.setattr(Account, "create_with_mnemonic", create)
    response = rpc(
        client,
        wallet,
        "tools/call",
        {"name": "hyperliquid_generate_wallet", "arguments": {"seed": "x" * 70000}},
    )
    assert response.status_code == 413
    create.assert_not_called()


def test_wallet_generation_omitted_arguments(client, wallet):
    response = rpc(client, wallet, "tools/call", {"name": "hyperliquid_generate_wallet"})
    assert response.status_code == 200
    assert not response.json()["result"].get("isError", False)
    data = json.loads(response.json()["result"]["content"][0]["text"])["data"]
    assert Account.from_key(data["privateKey"]).address == data["address"]


# Public BIP-39 test vector. Never fund this wallet.
SEED_VECTOR = "abandon " * 11 + "about"


@pytest.mark.parametrize(
    "phrase",
    [
        SEED_VECTOR,
        SEED_VECTOR.upper(),
        "  " + SEED_VECTOR.replace(" ", "   ") + "  ",
        "abandon " * 23 + "art",
    ],
)
def test_seed_auth_resolves_account(client, fake_sdk, phrase, caplog):

    account = Account.from_mnemonic(" ".join(phrase.lower().split()))
    if len(phrase.split()) == 12:
        assert account.address == "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    result = call(
        client,
        None,
        "hyperliquid_get_account",
        {"walletAccount": {"index": 0, "address": account.address}},
        headers={"Authorization": "Bearer " + phrase},
    )
    assert not result.get("isError", False)
    data = json.loads(result["content"][0]["text"])
    assert data["userAddress"] == account.address
    assert phrase not in caplog.text
    with pytest.raises(LookupError):
        current_wallet.get()


@pytest.mark.parametrize(
    "phrase",
    [
        "abandon " * 12,  # Valid words, invalid checksum.
        "abandon " * 11 + "notaword",
        "aban " * 11 + "abou",  # No abbreviated words.
        "abandon " * 14 + "address",  # Unsupported length, even when BIP-39 valid.
        "abandon " * 11,
        SEED_VECTOR + " extra",
        "x" * 513,
    ],
)
def test_invalid_seed_rejected_before_dispatch(client, monkeypatch, phrase, caplog):
    execute = Mock()
    monkeypatch.setattr(server, "execute_tool", execute)
    response = rpc(client, None, extra_headers={"Authorization": "Bearer " + phrase})
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert "12/24-word English BIP-39" in response.text
    assert phrase not in response.text + caplog.text
    execute.assert_not_called()


def test_generated_phrase_and_key_authenticate_same_wallet(client, fake_sdk, caplog):

    result = call(client, None, "hyperliquid_generate_wallet")
    data = json.loads(result["content"][0]["text"])["data"]
    assert set(data) == {"address", "privateKey", "seedPhrase"}
    assert len(data["seedPhrase"].split()) == 12
    assert Account.from_mnemonic(data["seedPhrase"]).key == Account.from_key(data["privateKey"]).key
    for credential in [data["seedPhrase"], data["privateKey"], data["privateKey"][2:]]:
        result = call(
            client,
            None,
            "hyperliquid_get_account",
            {"walletAccount": {"index": 0, "address": data["address"]}}
            if credential == data["seedPhrase"]
            else {},
            headers={"Authorization": "Bearer " + credential},
        )
        assert not result.get("isError", False)
        assert json.loads(result["content"][0]["text"])["userAddress"] == data["address"]
        assert credential not in caplog.text
