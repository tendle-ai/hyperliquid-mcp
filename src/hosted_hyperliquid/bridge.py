"""Legacy bridge USDC deposits and withdrawals using the request wallet.

Addresses and flow verified 2026-09-26:
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/usdc
"""

import os
import re
from decimal import Decimal
from urllib.parse import urlsplit

import requests
from eth_utils import is_address, is_checksum_address, to_checksum_address
from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL

from .trading import Trading

NETWORKS = {
    MAINNET_API_URL: (
        42161,
        "Arbitrum One",
        "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "0x2df1c51e09aecf9cacb7bc98cb1742757f163df7",
    ),
    TESTNET_API_URL: (
        421614,
        "Arbitrum Sepolia",
        "0x1baAbB04529D43a73232B713C0FE471f7c7334d5",
        "0x08cfc1B6b2dCF36A1480b99353A354AA8AC56f89",
    ),
}


class DepositError(Exception):
    """Messages are static and safe to return to the caller."""


def build_deposit(wallet_address, account_address, vault_address, base_url, amount):
    if vault_address or account_address.lower() != wallet_address.lower():
        raise DepositError(
            "Use the funded account's own key without a vault override; this bridge credits the sending wallet."
        )
    if base_url not in NETWORKS:
        raise DepositError("Unsupported deposit network.")
    if not isinstance(amount, str) or not re.fullmatch(r"[0-9]{1,78}(?:\.[0-9]{1,6})?", amount):
        raise DepositError("Amount must be a decimal string with at most six decimal places.")
    whole, _, fraction = amount.partition(".")
    units = int(whole) * 1_000_000 + int(fraction.ljust(6, "0"))
    if units < 5_000_000 or units >= 2**256:
        raise DepositError("Amount must be at least 5 USDC and fit uint256.")
    chain_id, _network, token, bridge = NETWORKS[base_url]
    # ERC-20 transfer(address,uint256): fixed selector, two 32-byte ABI words.
    calldata = "0xa9059cbb" + bridge[2:].lower().zfill(64) + format(units, "064x")
    return (
        chain_id,
        token,
        bridge,
        units,
        {"from": wallet_address, "to": token, "value": "0x0", "data": calldata},
    )


def submit_deposit(wallet, account_address, vault_address, base_url, amount):
    chain, token, bridge, units, call = build_deposit(
        wallet.address, account_address, vault_address, base_url, amount
    )
    default_rpc = (
        "https://arb1.arbitrum.io/rpc" if chain == 42161 else "https://sepolia-rollup.arbitrum.io/rpc"
    )
    rpc_url = os.getenv("ARBITRUM_RPC_URL") or default_rpc
    parsed_rpc = urlsplit(rpc_url)
    if parsed_rpc.scheme != "https" or not parsed_rpc.hostname:
        raise DepositError("ARBITRUM_RPC_URL must be an HTTPS endpoint.")
    with requests.Session() as session:

        def rpc(method, params):
            response = session.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=10,
                allow_redirects=False,
            )
            response.raise_for_status()
            body = response.json()
            if body.get("error") or "result" not in body:
                raise DepositError("Arbitrum RPC request failed.")
            return body["result"]

        try:
            if int(rpc("eth_chainId", []), 16) != chain:
                raise DepositError("RPC chain does not match the configured server network.")
            balance_data = "0x70a08231" + wallet.address[2:].lower().zfill(64)
            balance = int(rpc("eth_call", [{"to": token, "data": balance_data}, "pending"]), 16)
            if balance < units:
                raise DepositError(
                    "Insufficient USDC on the configured Arbitrum network; no transaction submitted."
                )
            if int(rpc("eth_call", [call, "pending"]), 16) != 1:
                raise DepositError("USDC transfer simulation failed; no transaction submitted.")
            gas = (int(rpc("eth_estimateGas", [call]), 16) * 120 + 99) // 100
            gas_price = int(rpc("eth_gasPrice", []), 16)
            if gas <= 0 or gas_price <= 0:
                raise DepositError("Invalid gas estimate; no transaction submitted.")
            if int(rpc("eth_getBalance", [wallet.address, "pending"]), 16) < gas * gas_price:
                raise DepositError("Insufficient ETH for Arbitrum gas; no transaction submitted.")
            nonce = int(rpc("eth_getTransactionCount", [wallet.address, "pending"]), 16)
            signed = wallet.sign_transaction(
                {
                    "chainId": chain,
                    "nonce": nonce,
                    "to": to_checksum_address(token),
                    "value": 0,
                    "data": call["data"],
                    "gas": gas,
                    "gasPrice": gas_price,
                }
            )
        except DepositError:
            raise
        except Exception:  # noqa: BLE001 -- redact provider and signing errors before submission
            raise DepositError("Deposit preflight failed; no transaction submitted.") from None

        tx_hash = "0x" + signed.hash.hex().removeprefix("0x")
        data = {
            "transactionHash": tx_hash,
            "chainId": chain,
            "creditedAccount": wallet.address,
            "amountBaseUnits": str(units),
            "tokenAddress": token,
            "bridgeAddress": bridge,
            "route": "legacy-arbitrum-bridge",
            "hyperliquidCreditConfirmed": False,
            "warning": "This bridge is deprecated; Hyperliquid prefers CCTP.",
        }
        # Broadcast exactly once. A timeout may mean the node accepted it.
        # Always retain the locally computed hash so callers can reconcile.
        try:
            returned_hash = rpc(
                "eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex().removeprefix("0x")]
            )
            if returned_hash.lower() != tx_hash.lower():
                raise DepositError("RPC returned an unexpected transaction hash.")
        except Exception:  # noqa: BLE001 -- preserve hash for all ambiguous broadcast failures
            data.update(
                status="unknown",
                error="Broadcast outcome unknown. Check transactionHash before any retry; another deposit call can transfer funds again.",
            )
            return {"message": "Deposit submission needs reconciliation.", "data": data}
        try:
            receipt = rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt is None:
                data["status"] = "submitted"
            elif receipt["transactionHash"].lower() != tx_hash.lower() or int(receipt["status"], 16) not in (
                0,
                1,
            ):
                raise ValueError("Invalid receipt")
            elif int(receipt["status"], 16) == 0:
                data.update(status="reverted", error="Deposit transaction reverted on Arbitrum.")
            else:
                data.update(status="confirmed", blockNumber=receipt["blockNumber"])
        except Exception:  # noqa: BLE001 -- receipt failure cannot undo successful submission
            data["status"] = "submitted"
            data["receiptCheck"] = "unavailable"
        return {
            "message": "Check status and transactionHash. Arbitrum confirmation does not prove Hyperliquid credit. Do not repeat the deposit to poll status.",
            "data": data,
        }


class WithdrawalError(Exception):
    """Static, credential-safe withdrawal validation messages."""


def submit_withdrawal(handler, arguments):
    if handler.vault_address or handler.account_address.lower() != handler.wallet.address.lower():
        raise WithdrawalError(
            "Withdrawals require the account owner's key without account or vault overrides."
        )
    amount = Decimal(arguments["amount"])
    if not amount.is_finite() or amount <= 0:
        raise WithdrawalError("Withdrawal amount must be positive.")
    destination = arguments.get("destination", handler.wallet.address)
    if not is_address(destination) or int(destination, 16) == 0:
        raise WithdrawalError("Withdrawal destination must be a nonzero EVM address.")
    body = destination[2:]
    if body != body.lower() and body != body.upper() and not is_checksum_address(destination):
        raise WithdrawalError("Withdrawal destination has an invalid mixed-case checksum.")
    destination = to_checksum_address(destination)
    # The pinned SDK serializes with str(amount); keep decimal precision intact.
    amount_text = format(amount, "f")
    response = Trading(handler).submit("withdraw_from_bridge", amount_text, destination)
    status = {"ok": "submitted", "err": "rejected"}.get(response.get("status"), "unknown")
    data = {
        "amount": amount_text,
        "destination": destination,
        "arrivalConfirmed": False,
        "status": status,
        "exchangeResponse": response,
    }
    if status != "submitted":
        data["error"] = (
            "Withdrawal rejected."
            if status == "rejected"
            else "Withdrawal outcome unknown; check withdrawal history and destination balance before retrying."
        )
    return {
        "message": "Withdrawal request processed; inspect status. Submission does not confirm arrival on Arbitrum. The bridge fee applies.",
        "data": data,
    }
