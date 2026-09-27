"""Request-scoped dispatch; one implementation per public operation."""

import time
from functools import cached_property

from eth_account import Account
from hyperliquid.utils.types import Cloid

from .account_reads import read_account, trading_capacity
from .auth import WALLET_PATH
from .bridge import submit_deposit, submit_withdrawal
from .catalog import WRITE_TOOLS
from .collateral import set_account_mode, transfer_collateral
from .markets import read_markets
from .read_validation import ReadInputError, market_history, order_book, order_status, records, vault_detail
from .sdk_info import RequestExchange as Exchange
from .sdk_info import RequestInfo as Info
from .trading import Trading
from .vaults import list_vaults


def generate_wallet():
    wallet, phrase = Account.create_with_mnemonic(num_words=12, account_path=WALLET_PATH)
    return {
        "data": {
            "address": wallet.address,
            "privateKey": "0x" + wallet.key.hex().removeprefix("0x"),
            "seedPhrase": phrase,
        }
    }


class HyperliquidTools:
    def __init__(self, identity, base_url, timeout=10.0):
        self.wallet, self.account_address, self.vault_address = (
            identity.wallet,
            identity.account_address,
            identity.vault_address,
        )
        self.base_url, self.timeout = base_url, timeout
        self.trading_dexs = set()

    @cached_property
    def info(self):
        # Reads use exact API symbols, so eager SDK symbol maps are unnecessary.
        return Info(
            self.base_url,
            skip_ws=True,
            timeout=self.timeout,
            meta={"universe": []},
            spot_meta={"tokens": [], "universe": []},
        )

    @cached_property
    def exchange(self):
        return Exchange(
            self.wallet,
            self.base_url,
            perp_dexs=sorted({"", *self.trading_dexs}) if self.trading_dexs - {""} else None,
            meta=None if self.trading_dexs else {"universe": []},
            spot_meta=None if self.trading_dexs else {"tokens": [], "universe": []},
            account_address=self.account_address,
            vault_address=self.vault_address,
            timeout=self.timeout,
        )

    def close(self):
        if "info" in self.__dict__:
            self.info.session.close()
        if "exchange" in self.__dict__:
            self.exchange.session.close()
            self.exchange.info.session.close()

    def execute(self, name, args):
        if "endTime" in args and args["endTime"] < args["startTime"]:
            raise ReadInputError("endTime must be greater than or equal to startTime.")
        if name == "hyperliquid_deposit":
            return submit_deposit(
                self.wallet, self.account_address, self.vault_address, self.base_url, args["amount"]
            )
        if name == "hyperliquid_withdraw":
            return submit_withdrawal(self, args)
        if name == "hyperliquid_set_account_mode":
            return set_account_mode(self, args)
        if name == "hyperliquid_transfer_collateral":
            return transfer_collateral(self, args)
        if name in WRITE_TOOLS:
            return Trading(self).execute(name, args)
        if name in {"hyperliquid_list_markets", "hyperliquid_market_details"}:
            result = read_markets(
                self.base_url, args, self.timeout, details=name == "hyperliquid_market_details"
            )
            if name == "hyperliquid_market_details" and args.get("includeAccount", False):
                result["accountTrading"] = trading_capacity(
                    self, result, args.get("userAddress", self.vault_address or self.account_address)
                )
            return result
        if name == "hyperliquid_list_vaults":
            return list_vaults(self.base_url, args, self.timeout)
        if name == "hyperliquid_vault_details":
            data = self.info.post("/info", {"type": "vaultDetails", "vaultAddress": args["vaultAddress"]})
            if data is None:
                raise ReadInputError("Vault not found on the configured network.")
            vault_detail(data, args["vaultAddress"])
            return {"data": data}
        user = args.get("userAddress", self.vault_address or self.account_address)
        if name == "hyperliquid_get_order_status":
            data = (
                self.info.query_order_by_oid(user, args["oid"])
                if "oid" in args
                else self.info.query_order_by_cloid(user, Cloid(args["cloid"]))
            )
            order_status(data)
            return {"data": data}
        if name == "hyperliquid_get_user_funding":
            data = self.info.user_funding_history(user, args["startTime"], args.get("endTime"))
            records(data, ("time", "delta"))
            return {"data": data, "completeHistory": False}
        if name in {
            "hyperliquid_get_order_book",
            "hyperliquid_get_historical_funding",
            "hyperliquid_get_candles",
        }:
            market = Trading(self).resolve(
                args, perp_only=name == "hyperliquid_get_historical_funding", allow_delisted=True
            )
            coin = market["symbol"]
            if name == "hyperliquid_get_order_book":
                data = self.info.post("/info", {"type": "l2Book", "coin": coin})
                order_book(data)
            elif name == "hyperliquid_get_historical_funding":
                payload = {"type": "fundingHistory", "coin": coin, "startTime": args["startTime"]}
                if "endTime" in args:
                    payload["endTime"] = args["endTime"]
                data = self.info.post("/info", payload)
                market_history(data)
            else:
                data = self.info.post(
                    "/info",
                    {
                        "type": "candleSnapshot",
                        "req": {
                            "coin": coin,
                            "interval": args["interval"],
                            "startTime": args["startTime"],
                            "endTime": args.get("endTime", int(time.time() * 1000)),
                        },
                    },
                )
                market_history(data, candles=True)
            return {"marketId": args["marketId"], "data": data}
        return read_account(self, name, args, user)
