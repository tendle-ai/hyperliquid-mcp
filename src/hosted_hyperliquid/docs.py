"""One plain-text document for HTTP and MCP, with a schema-derived tool reference."""

import json

from .catalog import CATALOG

VERSION = "1.9.0"

PUBLIC_TOOLS = frozenset({"hyperliquid_generate_wallet", "hyperliquid_get_docs"})

GUIDE = """## Connection and wallet setup

Tool names below use the hyperliquid_ prefix. Read the configured network at the top of this
document; it is server-wide, not a tool input. Refresh tools/list after updates.
Initialization, tools/list, get_docs and generate_wallet are public. Other
tools need Authorization: Bearer <credential> from secure client credentials.
Accept a 64-hex private key (optional 0x) or valid 12/24-word English BIP-39 phrase.
Normalize phrase whitespace to single spaces before putting it in the header.
Seed credentials do not select an account automatically. Call list_wallet_accounts
(no secrets in arguments), show the returned public addresses, and ask the user to
choose. Copy the chosen {index,address} into walletAccount on every other authenticated
tool call, including reads. The server validates the pair against the current phrase.
The agent/client remembers the user's choice; the server stores no selection. If the
choice is lost, ambiguous, or mismatches new credentials, ask again; never default to
index 0 or pick the richest account. Explicit existing user choice can be reused.
Discovery defaults to 10 addresses; follow nextIndex only as needed. These include
unused addresses, not all used/funded wallets; no balances are fetched. It covers
m/44'/60'/0'/0/index with no extra passphrase, not other wallet derivation conventions.
Other paths/passphrases require importing that account's private key instead.
Private-key credentials identify one account: discovery returns it; omit walletAccount.
Selection uses ordinary tool arguments; do not ask the user to paste secrets in chat
or modify authentication headers to switch accounts. Confirm get_account.userAddress
before funding. userAddress and account/vault overrides do not select a derived signer.
These checks verify credential/account consistency, not proof of human consent;
the agent must obtain the user's selection and separate authorization for actions.
Invalid supplied credentials are rejected even on public calls. generate_wallet
returns address, privateKey and a 12-word seedPhrase for the same account; save
both secrets securely without echoing them in chat/logs. Tool callers can see both. It does not fund/register the wallet; retries create
new wallets. Never put keys in URLs or tool arguments. userAddress selects a read
subject, not a signing account. Account/vault header overrides require existing
exchange authorization; owner-only tools reject overrides.

## Fresh evidence
Always query fresh balances, positions, orders, fills, prices, markets and settlement.
Refresh relevant reads before authorized writes and verify results afterward.
Failed reads and null values mean unavailable, not zero; never fill gaps from memory.
All timestamps are Unix milliseconds. Prices/amounts are decimal strings; quantities
are token units unless a field explicitly says otherwise. Tools do not authorize
actions: follow the user's scope. Never invent a rejection cause or recommend a
state-changing fix as necessary without evidence. Report the exchange's actual error.

## Find the exact market
list_markets searches symbols, UI display names, deployment names and upstream
keywords by case-insensitive substring. Company-name aliases come from Hyperliquid's
perpConciseAnnotations/tokenConciseAnnotations, not an inferred alias list. Use
search="anthropic" or search="openai"; use category="preipo" for upstream pre-IPO
classifications. Available categories are returned for the selected scope before
search/category filters. Null category means unclassified, not ineligible. Upstream
labels/keywords can be incomplete: no match does not prove no exposure exists.
Failed annotation reads return errors, not empty results. Search is not typo-tolerant.
limit=20 is a page size; follow nextOffset. dex restricts to perps; omit it to search
all deployments and spot. Delisted markets are hidden by default; includeDelisted=true
can explain an old listing. Market counts and categories are live, never fixed.
Copy marketId into market_details and orders: perp:BTC is default perps;
perp:xyz:SP500 is a market on builder deployment xyz; spot:@1 is a spot identifier
(example only). SP500 is a market, xyz is the deployment; neither is another wallet.
Default perps, builder (HIP-3) perps and spot are supported by order tools. Similar
names/prices do not establish the same underlying asset. supportedByOrderTools is
connector capability, not account eligibility; tradingEnabled=null means unknown.

## Balances and capacity
get_account returns positions for one dex plus balanceLocations: spot balances/holds
and unique perp deployment entries. Each spot row has availableAfterMaintenance
(exchange-reported, null if missing), held (exchange hold), total (includes held),
and unheld (total minus held). Lead with availableAfterMaintenance when describing
available funds; never present total as spending power. Do not infer the cause of a
hold. unheld is arithmetic, not guaranteed trading/withdrawal capacity, and must not
replace missing maintenance availability. Zero perp totals in shared mode do not
mean unfunded perps or prove that nothing moved.
A selected builder also includes default-perp
balances, but not its positions. Query each relevant dex before declaring the whole
account flat. These are Hyperliquid balances, not Arbitrum ETH/USDC holdings.
accountMode=disabled means Standard with separate balances; unifiedAccount shares
each collateral asset across spot and compatible cross-margin perps. It does not
convert USDC into another collateral token. portfolioMargin is a distinct mode.
Raw default/dexAbstraction are treated as unverified by this server, not as Standard.
Respect balanceLocations.interpretation; never sum reported perp totals with shared
spot balances. Account abstraction is separate from a position's cross/isolated mode.
market_details(includeAccount=true) returns perp accountTrading: availableToTrade
in collateral units, maxTradeSzs in base units, sideOrder=[buy,sell], current leverage.
Use these fresh estimates, not total equity, to assess a specific trade. Zero does
not establish its cause. Spot needs the appropriate base/quote balance and holds;
this server does not return spot trading capacity. Portfolio history is not cash.

## Funding and mode changes
transfer_collateral moves funds within the owner's wallet: source/destination are
"spot", "" (default perps), or exact builder dex such as "xyz". tokenIndex comes from
market_details.collateral.index and must match every involved perp deployment.
Example shape: {"source":"","destination":"xyz","tokenIndex":0,"amount":"2"};
verify the token and amount, never copy example values as authorization. This funds
builder collateral in Standard mode without changing mode. It is not an external
deposit, withdrawal, token swap or trade. Shared modes return not_applicable and
submitted=false. Before/after reads cover the selected locations and do not prove
transaction-specific settlement. Re-read ledger/balances and target-market capacity.
set_account_mode(mode=unified|standard) is an explicitly authorized account-wide
change; never use it as an incidental trade prerequisite. Targets map to API
unifiedAccount|disabled. already_configured means no write; verification reports
observed mode. A rejection does not prove an open position caused it. Do not close
positions or promise a reversible round trip without evidence and authorization.
Read balances/positions/capacity again after changing mode; do not assume balances
return to earlier locations. Portfolio-margin transitions are not exposed.
Owner keys without account/vault overrides are required for both tools and bridges.

## Trade and verify
place_orders, modify_orders and cancel_orders take orders arrays even for one item.
size is base quantity, not dollars or margin; leverage does not multiply the size
input. market orders use bounded-slippage IOC limits and can fill partly or not at
all. limit/trigger orders require price; triggerPrice is a separate activation price.
place_bracket_order creates linked entry plus reduce-only LIMIT TP/SL exits; reaching
a trigger does not guarantee a fill. Spot has no leverage, triggers or reduce-only.
Use market_details precision and margin tiers; maxLeverage is a ceiling, not the
account setting. Do not infer all listed markets are affordable or apply a blanket
spot minimum: the exchange enforces spot notional rules. Fees and holds affect funds.
update_leverage sets leverage and cross/isolated mode, not exposure. close_position
uses reduce-only IOC; verify fills and remaining position. Closing does not reset
leverage configuration. update_isolated_margin adjusts existing isolated collateral.
cancel_all_orders covers one perp dex; schedule_cancel cancels orders, not positions.

## Outcomes and recovery
Read data.status and per-item response.data.statuses, not only MCP isError. A batch
can partly succeed even when isError=true. ok is an exchange acknowledgement;
resting means open, filled reports execution, and error means that item was rejected.
submitted is not settlement or a verified setting. unknown may have executed: never
repeat a write to poll. Reconcile orders with returned clientOrderIds, get_order_status,
get_orders and get_user_fills; client IDs do not make retries idempotent. unknownOid
and bounded-history absence are not proof of nonexecution. get_orders open is scoped
to one perp dex; history and other history tools are API-bounded, not complete archives.
Readback failures do not undo accepted writes; balance observations are not receipts.

deposit sends USDC from Arbitrum via the legacy bridge (minimum 5 USDC, ETH gas).
It checks funds internally but there is no standalone Arbitrum balance/receipt tool.
confirmed refers to the Arbitrum receipt, not Hyperliquid credit; preserve transactionHash.
withdraw submits a bridge withdrawal with a fee; arrivalConfirmed=false means arrival
has not been verified. get_ledger_updates is Hyperliquid evidence, not an Arbitrum
receipt. Use an external chain read for settlement when needed; never repeat either
bridge call to check status. The legacy deposit bridge is deprecated; CCTP is not
implemented by this server. list_vaults discovers addresses; vault_details inspects
one vault. Historical APR is not a forecast; neither tool transfers vault funds.
"""


GROUPS = {
    "Setup and wallets": {"get_docs", "generate_wallet", "list_wallet_accounts"},
    "Account reads": {
        "get_account",
        "get_orders",
        "get_order_status",
        "get_user_fills",
        "get_user_funding",
        "get_ledger_updates",
        "get_portfolio",
    },
    "Market and vault reads": {
        "list_markets",
        "market_details",
        "get_order_book",
        "get_historical_funding",
        "get_candles",
        "list_vaults",
        "vault_details",
    },
    "Trading": {
        "place_orders",
        "modify_orders",
        "cancel_orders",
        "cancel_all_orders",
        "place_bracket_order",
        "close_position",
        "update_leverage",
        "update_isolated_margin",
        "schedule_cancel",
    },
    "Collateral and bridges": {"set_account_mode", "transfer_collateral", "deposit", "withdraw"},
}


def build_docs(network, public_url):
    base = public_url.rstrip("/")
    parts = [
        f"# Hyperliquid MCP documentation\n\nVersion: {VERSION}\nNetwork: {network}\nTransport: Streamable HTTP\nMCP: {base}/mcp\nDocs: {base}/mcp/docs\nMetadata: {base}/manifest.json\n\nSupports default perps, builder/HIP-3 perps and spot. Financial tools can move real funds on mainnet.",
        GUIDE.strip(),
        """## Common workflows\n\nConnect: read get_docs → store credentials securely → list_wallet_accounts → ask the user to choose → get_account. Private keys already select one wallet.\nTrade: list_markets → market_details(includeAccount=true) → inspect fresh balances/orders → obtain action authorization → place_orders → verify order status, fills and remaining position.\nBridge: confirm network, amount and authorization → deposit or withdraw once → reconcile returned status with fresh ledger/chain reads. Never resubmit to poll.\n\nExamples (tool arguments only; credentials stay in secure headers):\n\nlist_markets: {"search":"anthropic"}\nlist_wallet_accounts: {"startIndex":0,"limit":10}\nmarket_details: {"marketId":"perp:BTC","includeAccount":true}\n\nWith seed credentials, add the user's chosen walletAccount to market/account/trading calls. Example queries do not authorize writes.""",
        "## Tool reference\n\nTool names below are exact. Inputs are JSON objects. Required arrays and conditional rules in each schema define required fields; additionalProperties=false rejects unknown inputs. Defaults describe server behavior, not client authorization. Amounts and sizes use the units stated in descriptions.\n\nCommon optional schema field: walletAccount. Required at runtime with seed credentials on every authenticated tool except list_wallet_accounts; omit with private keys. It is shown once here to avoid repeating it in every tool schema:",
    ]
    common = next(
        t["inputSchema"]["properties"]["walletAccount"]
        for t in CATALOG
        if "walletAccount" in t["inputSchema"]["properties"]
    )
    parts.append("```json\n" + json.dumps(common, indent=2) + "\n```")
    for group, names in GROUPS.items():
        parts.append("### " + group)
        for item in CATALOG:
            if item["name"].removeprefix("hyperliquid_") not in names:
                continue
            schema = dict(item["inputSchema"])
            props = schema["properties"]
            has_selection = "walletAccount" in props
            schema["properties"] = {k: v for k, v in props.items() if k != "walletAccount"}
            access = "Public" if item["name"] in PUBLIC_TOOLS else "Authenticated"
            effect = (
                "Read-only"
                if item["annotations"]["readOnlyHint"]
                else "Creates a new wallet"
                if item["name"] == "hyperliquid_generate_wallet"
                else "Changes funds, orders or account settings"
            )
            parts.append(
                f"#### {item['name']}\n\n{access}. {effect}.\n\n{item['description']}"
                + ("\n\nAlso accepts the common walletAccount field above." if has_selection else "")
                + "\n\nInput schema:\n```json\n"
                + json.dumps(schema, indent=2)
                + "\n```"
            )
    return "\n\n".join(parts) + "\n"
