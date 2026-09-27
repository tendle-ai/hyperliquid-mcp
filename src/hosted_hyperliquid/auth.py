"""Private-key or seed-phrase authentication. Credentials exist only in request memory."""

import re
from contextvars import ContextVar
from dataclasses import dataclass

from eth_account import Account
from eth_account.hdaccount import Mnemonic
from eth_account.signers.local import LocalAccount
from eth_account.types import Language
from eth_utils import is_address, to_checksum_address
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

# Pinned eth-account HD API; validate full English words before derivation.
Account.enable_unaudited_hdwallet_features()
WALLET_PATH = "m/44'/60'/0'/0/0"
ENGLISH = Mnemonic(Language.ENGLISH)


def parse_credential(value):
    if len(value) > 512:
        raise ValueError()
    value = value.strip()
    if re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", value):
        wallet = Account.from_key(value)
        return WalletIdentity(wallet, wallet.address)
    phrase = " ".join(value.lower().split())
    if len(phrase.split()) not in (12, 24) or not ENGLISH.is_mnemonic_valid(phrase):
        raise ValueError()
    return WalletIdentity(None, None, seed_phrase=phrase)


@dataclass(frozen=True, repr=False)
class WalletIdentity:
    wallet: LocalAccount | None
    account_address: str | None
    vault_address: str | None = None
    seed_phrase: str | None = None


class WalletSelectionError(ValueError):
    pass


def derive_account(identity, index):
    return Account.from_mnemonic(
        identity.seed_phrase, account_path=f"{WALLET_PATH.rsplit('/', 1)[0]}/{index}"
    )


def list_wallet_accounts(identity, arguments):
    start, limit = arguments.get("startIndex", 0), arguments.get("limit", 10)
    if identity.seed_phrase is None:
        if start != 0:
            raise WalletSelectionError("Private-key credentials identify one wallet; use startIndex=0.")
        return {
            "credentialType": "privateKey",
            "accounts": [{"address": identity.wallet.address}],
            "nextIndex": None,
            "note": "One signing wallet. Omit walletAccount on subsequent calls. Account/vault overrides are separate.",
        }
    end = min(start + limit, 2**31)
    return {
        "credentialType": "seedPhrase",
        "accounts": [{"index": i, "address": derive_account(identity, i).address} for i in range(start, end)],
        "nextIndex": end if end < 2**31 else None,
        "note": "Derived Ethereum addresses, including unused ones; not a list of funded or previously used accounts. No balances checked. Only m/44'/60'/0'/0/index, no extra passphrase. Ask the user to choose an address, then copy its index/address as walletAccount on each authenticated call. No account is selected or remembered by this tool. Other wallet paths require the account's private key. Never choose based on balance alone.",
    }


def select_wallet(identity, selection):
    if identity.seed_phrase is None:
        if selection is not None:
            raise WalletSelectionError(
                "Private-key credentials already select one wallet; omit walletAccount."
            )
        return identity
    if selection is None:
        raise WalletSelectionError(
            "Wallet selection required. Call hyperliquid_list_wallet_accounts, ask the user to choose, then pass walletAccount={index,address} on each authenticated call. No action submitted."
        )
    wallet = derive_account(identity, int(selection["index"]))
    if wallet.address.lower() != selection["address"].lower():
        raise WalletSelectionError(
            "walletAccount address does not match its index under the current credential. Refresh hyperliquid_list_wallet_accounts and confirm the selection. No action submitted."
        )
    return WalletIdentity(wallet, identity.account_address or wallet.address, identity.vault_address)


current_wallet: ContextVar[WalletIdentity | None] = ContextVar("current_wallet")


class WalletAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)

        headers = scope.get("headers", [])
        credentials = [value for name, value in headers if name.lower() == b"authorization"]
        identity = None
        try:
            if len(credentials) > 1:
                raise ValueError()
            if credentials:
                scheme, key = credentials[0].decode("ascii").split(" ", 1)
                if scheme.lower() != "bearer":
                    raise ValueError()
                identity = await run_in_threadpool(parse_credential, key)
        except (ValueError, TypeError, UnicodeError):
            response = JSONResponse(
                {
                    "error": "Invalid Authorization header. Configure Bearer with a 32-byte hex private key or valid 12/24-word English BIP-39 seed phrase (no extra passphrase; choose an account using list_wallet_accounts) in secure credentials, or omit the header for public tools."
                },
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="hyperliquid-mcp"', "Cache-Control": "no-store"},
            )
            return await response(scope, receive, send)

        targets = {}
        for header, field_name in (
            (b"x-hyperliquid-account-address", "account"),
            (b"x-hyperliquid-vault-address", "vault"),
        ):
            values = [value for name, value in headers if name.lower() == header]
            try:
                if len(values) > 1:
                    raise ValueError()
                if values:
                    value = values[0].decode("ascii")
                    if not is_address(value):
                        raise ValueError()
                    targets[field_name] = to_checksum_address(value)
            except (ValueError, UnicodeError):
                return await JSONResponse(
                    {"error": "Invalid account or vault address header."}, status_code=400
                )(scope, receive, send)

        if identity is not None:
            identity = WalletIdentity(
                identity.wallet,
                targets.get("account", identity.account_address),
                targets.get("vault"),
                identity.seed_phrase,
            )
        context_token = current_wallet.set(identity)
        # Downstream protocol handling and tracing never receive the credential header.
        clean_scope = dict(scope)
        clean_scope["headers"] = [
            (name, value) for name, value in headers if name.lower() != b"authorization"
        ]

        async def no_cache(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [(b"cache-control", b"no-store")]
            await send(message)

        try:
            await self.app(clean_scope, receive, no_cache)
        finally:
            current_wallet.reset(context_token)
