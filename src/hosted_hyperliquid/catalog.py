"""The single public contract. Shared schemas avoid drift between order operations."""


def obj(properties, required=(), **rules):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
        **rules,
    }


def enum(*values):
    return {"type": "string", "enum": list(values)}


def integer(low=0, high=9007199254740991):
    return {"type": "integer", "minimum": low, "maximum": high}


def array(items, maximum=50):
    return {"type": "array", "items": items, "minItems": 1, "maxItems": maximum}


ADDRESS = {"type": "string", "pattern": r"^0x[0-9a-fA-F]{40}$", "minLength": 42, "maxLength": 42}
MARKET = {"type": "string", "pattern": r"^(perp|spot):[^\x00-\x1f]+(?![\s\S])", "maxLength": 256}
DECIMAL = {"type": "string", "pattern": r"^[0-9]{1,15}(\.[0-9]{1,18})?$", "maxLength": 34}
CLOID = {"type": "string", "pattern": r"^0x[0-9a-fA-F]{32}$", "minLength": 34, "maxLength": 34}
DEX = {
    "type": "string",
    "maxLength": 64,
    "description": "Exact perp deployment; empty string selects default perps.",
}
SLIPPAGE = {"type": "number", "exclusiveMinimum": 0, "maximum": 0.1, "default": 0.05}
BOOL = {"type": "boolean"}
USER = {"userAddress": ADDRESS}
RANGE = {
    key: {**integer(), "description": "Unix timestamp in milliseconds."} for key in ("startTime", "endTime")
}
IDENTIFIER = {
    "oneOf": [
        {"required": ["oid"], "not": {"required": ["cloid"]}},
        {"required": ["cloid"], "not": {"required": ["oid"]}},
    ]
}
ORDER_FIELDS = {
    "marketId": MARKET,
    "type": enum("market", "limit", "trigger"),
    "isBuy": BOOL,
    "size": {
        **DECIMAL,
        "description": "Base-token quantity, not dollars or margin; leverage does not multiply this input.",
    },
    "price": DECIMAL,
    "timeInForce": enum("Gtc", "Ioc", "Alo"),
    "reduceOnly": BOOL,
    "slippage": SLIPPAGE,
    "triggerPrice": DECIMAL,
    "triggerKind": enum("tp", "sl"),
    "triggerIsMarket": BOOL,
}
ORDER_RULES = [
    {
        "if": {"properties": {"type": {"const": "market"}}},
        "then": {
            "not": {
                "anyOf": [
                    {"required": [x]}
                    for x in ["price", "timeInForce", "triggerPrice", "triggerKind", "triggerIsMarket"]
                ]
            }
        },
    },
    {
        "if": {"properties": {"type": {"const": "limit"}}},
        "then": {
            "required": ["price"],
            "not": {
                "anyOf": [
                    {"required": [x]} for x in ["slippage", "triggerPrice", "triggerKind", "triggerIsMarket"]
                ]
            },
        },
    },
    {
        "if": {"properties": {"type": {"const": "trigger"}}},
        "then": {
            "required": ["price", "triggerPrice", "triggerKind", "triggerIsMarket"],
            "not": {"anyOf": [{"required": ["slippage"]}, {"required": ["timeInForce"]}]},
        },
    },
]
ORDER = obj({**ORDER_FIELDS, "cloid": CLOID}, ["marketId", "type", "isBuy", "size"], allOf=ORDER_RULES)
MODIFY = obj(
    {**ORDER_FIELDS, "type": enum("limit", "trigger"), "oid": integer(), "cloid": CLOID},
    ["marketId", "type", "isBuy", "size"],
    allOf=[*ORDER_RULES, IDENTIFIER],
)
CANCEL = obj({"marketId": MARKET, "oid": integer(), "cloid": CLOID}, ["marketId"], **IDENTIFIER)


def tool(name, description, schema, *, write=False, external=True):
    if name not in {"get_docs", "generate_wallet", "list_wallet_accounts"}:
        schema["properties"]["walletAccount"] = {
            **obj({"index": integer(0, 2**31 - 1), "address": ADDRESS}, ["index", "address"]),
            "description": "Required with seed-phrase credentials: copy the user's chosen index/address from list_wallet_accounts. Omit with private-key credentials. Selects signer, not userAddress or account/vault overrides; never silently choose account zero.",
        }
    return {
        "name": "hyperliquid_" + name,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "readOnlyHint": not write,
            "destructiveHint": write,
            "idempotentHint": not write,
            "openWorldHint": external,
        },
    }


CATALOG = [
    tool(
        "get_docs",
        "Read the complete plain-text documentation: setup, wallet selection, workflows, errors, limitations and current tool schemas. Same text as /mcp/docs. Public; no credentials required.",
        obj({}),
        external=False,
    ),
    tool(
        "generate_wallet",
        "Generate an EVM wallet. Returns address/privateKey/seedPhrase (12 English BIP-39 words, first Ethereum account, no extra passphrase); server never stores them. The caller can see both secrets. Save securely, never echo keys into chat/logs. Each retry creates a different wallet. Public; does not fund or register it.",
        obj({}),
        write=True,
        external=False,
    ),
    tool(
        "list_wallet_accounts",
        "Discover signing addresses from secure credentials. Seed phrase: paginated Ethereum addresses, including unused ones; ask the user to select, then pass walletAccount={index,address} on other authenticated tools. Private key: one address, omit walletAccount. Returns no secrets or balances, changes no selection, does not enumerate all paths or Hyperliquid subaccounts.",
        obj(
            {
                "startIndex": {**integer(0, 2**31 - 1), "default": 0},
                "limit": {**integer(1, 10), "default": 10},
            }
        ),
        external=False,
    ),
    tool(
        "get_account",
        "Read raw accountMode, positions for one dex and per-token availableAfterMaintenance, held, total and unheld balances. Lead with availableAfterMaintenance (null if unavailable), never total as spending power. unheld is total minus held, not guaranteed trade/withdrawal capacity. balanceLocations contains unique default/selected perp balances with separate/shared/unverified interpretation. Raw default is unverified, not confirmed Standard. Shared perp totals are not additional funds. No Arbitrum balances; use market_details(includeAccount=true) for perp capacity. Optional include adds account metadata.",
        obj(
            {
                **USER,
                "dex": DEX,
                "include": {
                    **array(
                        enum(
                            "role",
                            "fees",
                            "subAccounts",
                            "agents",
                            "multiSigSigners",
                            "referrals",
                            "rateLimit",
                        ),
                        7,
                    ),
                    "uniqueItems": True,
                },
            }
        ),
    ),
    tool(
        "place_orders",
        "Submit 1–50 orders using exact marketId. All items are validated before one submission; individual results can differ. Default/builder perps and spot supported; spot has no triggers/reduce-only. Returned client IDs enable reconciliation after unknown outcomes; never blindly retry.",
        obj({"orders": array(ORDER)}, ["orders"]),
        write=True,
    ),
    tool(
        "modify_orders",
        "Modify 1–50 orders by oid or cloid and exact marketId. Requires an explicit limit or trigger order definition. Inspect every result; not atomic.",
        obj({"orders": array(MODIFY)}, ["orders"]),
        write=True,
    ),
    tool(
        "cancel_orders",
        "Cancel 1–50 specific orders by oid or cloid and exact marketId. Use one identifier type per batch. Inspect each result.",
        obj({"orders": array(CANCEL)}, ["orders"]),
        write=True,
    ),
    tool(
        "cancel_all_orders",
        "Cancel open perpetual orders on the selected deployment for the authenticated trading account. Explicit dex required; empty string means default perps. Does not close positions or cancel spot orders.",
        obj({"dex": DEX}, ["dex"]),
        write=True,
    ),
    tool(
        "place_bracket_order",
        "Submit linked perpetual entry, take-profit and stop-loss orders. Entry is a market or limit order; exit triggers are reduce-only limit orders. All three can have different outcomes; not atomic. No spot brackets.",
        obj(
            {"entry": ORDER, "takeProfitPrice": DECIMAL, "stopLossPrice": DECIMAL},
            ["entry", "takeProfitPrice", "stopLossPrice"],
        ),
        write=True,
    ),
    tool(
        "close_position",
        "Close all or part of a perp position using a fresh position lookup and reduce-only IOC. Optional size defaults to full position. Check fills and remaining position; partial fills are possible. Does not reset leverage configuration.",
        obj({"marketId": MARKET, "size": DECIMAL, "slippage": SLIPPAGE}, ["marketId"]),
        write=True,
    ),
    tool(
        "update_leverage",
        "Set perp leverage and cross/isolated mode. Exchange enforces margin tiers. Verifies current leverage with fresh account-specific market data, even without a position. Failed or mismatched readback never means the accepted action should be blindly retried.",
        obj(
            {"marketId": MARKET, "leverage": integer(1, 1000), "isCross": BOOL},
            ["marketId", "leverage", "isCross"],
        ),
        write=True,
    ),
    tool(
        "update_isolated_margin",
        "Adjust collateral on an existing isolated position, without changing size. Positive amount adds, negative removes. No deployment transfers.",
        obj(
            {
                "marketId": MARKET,
                "amount": {"type": "string", "pattern": r"^-?[0-9]{1,14}(\.[0-9]{1,6})?$", "maxLength": 22},
            },
            ["marketId", "amount"],
        ),
        write=True,
    ),
    tool(
        "schedule_cancel",
        "Schedule account-wide cancellation of open orders at Unix milliseconds at least five seconds ahead, or clear with time=null. Does not close positions. Exchange trigger limits apply.",
        obj({"time": {"type": ["integer", "null"], "minimum": 0, "maximum": 9007199254740991}}, ["time"]),
        write=True,
    ),
    tool(
        "get_orders",
        "Read open orders on one perp deployment, or API-bounded historical orders. History is account-wide: dex is not accepted with view=history. Absence in history does not prove an order never existed.",
        obj(
            {**USER, "view": enum("open", "history"), "dex": DEX},
            ["view"],
            allOf=[
                {"if": {"properties": {"view": {"const": "history"}}}, "then": {"not": {"required": ["dex"]}}}
            ],
        ),
    ),
    tool(
        "get_order_status",
        "Look up one order by exchange oid or client cloid. Use for uncertain submission reconciliation.",
        obj({**USER, "oid": integer(), "cloid": CLOID}, **IDENTIFIER),
    ),
    tool(
        "get_user_fills",
        "Read recent, time-bounded, or TWAP slice fills. Explicit mode required. API-bounded response, not guaranteed complete history.",
        obj(
            {**USER, **RANGE, "mode": enum("recent", "time", "twap"), "aggregateByTime": BOOL},
            ["mode"],
            allOf=[
                {
                    "if": {"properties": {"mode": {"const": "time"}}},
                    "then": {"required": ["startTime"]},
                    "else": {
                        "not": {
                            "anyOf": [{"required": [x]} for x in ["startTime", "endTime", "aggregateByTime"]]
                        }
                    },
                }
            ],
        ),
    ),
    tool(
        "get_user_funding",
        "Read account funding payments in a time range. API-bounded history.",
        obj({**USER, **RANGE}, ["startTime"]),
    ),
    tool(
        "get_ledger_updates",
        "Read Hyperliquid non-funding ledger history. API-bounded; not Arbitrum receipt or final bridge-settlement verification.",
        obj({**USER, **RANGE}, ["startTime"]),
    ),
    tool(
        "get_portfolio",
        "Read historical performance, optionally including user vault equities. Not spendable balances.",
        obj({**USER, "includeVaultEquities": BOOL}),
    ),
    tool(
        "list_markets",
        "Search default perps, builder perps and spot. Default limit=20 is a PAGE SIZE, not total markets. Inspect total/nextOffset, or search directly. Search matches symbols, UI display names and upstream keywords, not semantic guesses. Filter category (e.g. preipo); response categories lists available upstream labels. Missing labels remain unknown. Copy exact marketId to other tools. Supplying dex restricts to perps.",
        obj(
            {
                "search": {"type": "string", "maxLength": 200},
                "category": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": r"^\S+$",
                    "description": "Exact upstream category, case-insensitive; e.g. preipo, stocks, indices. See response categories. Unclassified markets are excluded when filtering.",
                },
                "marketType": enum("all", "perp", "spot"),
                "dex": DEX,
                "includeDelisted": BOOL,
                "limit": integer(1, 100),
                "offset": integer(0, 1000000),
            }
        ),
    ),
    tool(
        "market_details",
        "Get exact-market UI display name, keywords/category, prices, collateral or base/quote tokens, precision, leverage ceilings/tiers and listing status. includeAccount=true adds fresh perpetual available-to-trade amounts, maximum sizes and current leverage for the request account (or userAddress). tradingEnabled=null means unknown; listing is not trading permission.",
        obj(
            {"marketId": MARKET, "includeAccount": BOOL, **USER},
            ["marketId"],
            allOf=[
                {
                    "if": {"required": ["userAddress"]},
                    "then": {
                        "required": ["includeAccount"],
                        "properties": {"includeAccount": {"const": True}},
                    },
                }
            ],
        ),
    ),
    tool(
        "get_order_book",
        "Read the current order book for an exact default-perp, builder-perp or spot market.",
        obj({"marketId": MARKET}, ["marketId"]),
    ),
    tool(
        "get_historical_funding",
        "Read API-bounded funding history for an exact perpetual market. Spot is rejected.",
        obj({"marketId": MARKET, **RANGE}, ["marketId", "startTime"]),
    ),
    tool(
        "get_candles",
        "Read API-bounded OHLCV candles for an exact market. Times are Unix milliseconds.",
        obj(
            {"marketId": MARKET, "interval": enum("1m", "5m", "15m", "1h", "4h", "1d"), **RANGE},
            ["marketId", "interval", "startTime"],
        ),
    ),
    tool(
        "list_vaults",
        "Search vault directory summaries by name, address or leader. TVL descending, excludes closed vaults by default. Snapshots can change between pages.",
        obj(
            {
                "search": {"type": "string", "maxLength": 200},
                "limit": integer(1, 100),
                "offset": integer(),
                "includeClosed": BOOL,
            }
        ),
    ),
    tool(
        "vault_details",
        "Read detailed information for one known vault address. Use list_vaults to discover addresses. Does not move funds.",
        obj({"vaultAddress": ADDRESS}, ["vaultAddress"]),
    ),
    tool(
        "set_account_mode",
        "Explicit account-wide collateral configuration: unified shares compatible collateral; standard separates spot/perp deployment balances. Requires owner key without overrides and user authorization. Reads mode before/after; same target skips submission. Exchange restrictions may reject changes. Portfolio-margin transitions are not exposed. No automatic transfer or trade; verify mode, balances and target-market capacity afterward.",
        obj({"mode": enum("unified", "standard")}, ["mode"]),
        write=True,
    ),
    tool(
        "transfer_collateral",
        "Transfer collateral within the owner's wallet between spot, default perps and builder deployments. source/destination: spot, empty string for default perps, or exact builder dex such as xyz. tokenIndex comes from market_details collateral.index; token must match every involved perp deployment. Explicit positive decimal amount; owner key without overrides only. Shared modes return not_applicable without submission. Reads balances before/after; never changes account mode. Reconcile unknown outcomes without retrying; refresh market capacity after transfer.",
        obj(
            {
                "source": {
                    **DEX,
                    "description": "spot, empty string for default perps, or exact builder deployment.",
                },
                "destination": {
                    **DEX,
                    "description": "spot, empty string for default perps, or exact builder deployment.",
                },
                "tokenIndex": integer(),
                "amount": DECIMAL,
            },
            ["source", "destination", "tokenIndex", "amount"],
        ),
        write=True,
    ),
    tool(
        "deposit",
        "Sign and submit USDC through the legacy Hyperliquid bridge on the configured Arbitrum network. Minimum 5 USDC; ETH gas required. No account/vault overrides. Non-idempotent: inspect submitted/confirmed/reverted/unknown status; Arbitrum confirmation is not Hyperliquid credit. Never retry to poll. Legacy bridge is deprecated; CCTP preferred.",
        obj(
            {"amount": {"type": "string", "pattern": r"^[0-9]{1,78}(\.[0-9]{1,6})?$", "maxLength": 85}},
            ["amount"],
        ),
        write=True,
    ),
    tool(
        "withdraw",
        "Submit a USDC withdrawal through the legacy bridge to Arbitrum on the server network. Owner key only; no account/vault overrides. Destination defaults to signer. Fee applies. Returns submitted/rejected/unknown, not arrival confirmation. Never retry an unknown outcome without reconciliation.",
        obj(
            {
                "amount": {"type": "string", "pattern": r"^[0-9]{1,20}(\.[0-9]{1,6})?$", "maxLength": 27},
                "destination": ADDRESS,
            },
            ["amount"],
        ),
        write=True,
    ),
]
# Creating a wallet is non-idempotent, but does not destroy existing state.
CATALOG[1]["annotations"]["destructiveHint"] = False
WRITE_TOOLS = frozenset(t["name"] for t in CATALOG if t["annotations"]["destructiveHint"])
