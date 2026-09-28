# Hyperliquid MCP

Hosted Hyperliquid connector by Tendle. Repository: [tendleai/hyperliquid-mcp](https://github.com/tendleai/hyperliquid-mcp).

30 tools for default perps, builder/HIP-3 perps, spot, account reads, collateral
management and bridge submissions. One current contract; no legacy aliases.

## Connect

- MCP: https://hyperliquid.tendle.ai/mcp
- Product page: https://tendle.ai/connectors/hyperliquid
- Connector dictionary: https://tendle.ai/connectors/hyperliquid/manifest.json
- Icon: https://hyperliquid.tendle.ai/mcp/icon
- Docs: https://hyperliquid.tendle.ai/mcp/docs
- Health: https://hyperliquid.tendle.ai/healthz

The connector uses a dedicated hostname; its product page and manifest remain
in the Tendle catalog. Configure the MCP URL and permitted credential host as
`hyperliquid.tendle.ai`. Existing clients must update their endpoint and secure
credential binding; changing the catalog URL alone does not migrate a connection.
- Hosted network: **mainnet**. Transport: **Streamable HTTP**.

Initialization, tool discovery, `hyperliquid_get_docs` and
`hyperliquid_generate_wallet` are public. Other tools require
`Authorization: Bearer <credential>` through secure client credentials.
Invalid supplied credentials are rejected even on public operations. Keys are
request-scoped and never persisted by the server. Generated keys must be saved
securely by the client, never echoed into chat or logs.

`tools/list` supplies current schemas. `get_docs` returns the same plain text as the public docs endpoint, including
usage guidance and a tool reference generated from the current schemas. Refresh both after updates. These are the source
of detailed usage guidance; this README covers development and operations.

## Tools

Names below have the `hyperliquid_` prefix.

| Area | Tools |
| --- | --- |
| Setup | get_docs, generate_wallet, list_wallet_accounts |
| Account | get_account, get_orders, get_order_status, get_user_fills, get_user_funding, get_ledger_updates, get_portfolio |
| Markets | list_markets, market_details, get_order_book, get_historical_funding, get_candles |
| Trading | place_orders, modify_orders, cancel_orders, cancel_all_orders, place_bracket_order, close_position, update_leverage, update_isolated_margin, schedule_cancel |
| Collateral | set_account_mode, transfer_collateral |
| Bridge | deposit, withdraw |
| Vaults | list_vaults, vault_details |

Market execution uses exact discovered IDs. Discovery enriches names, keywords and
categories from live Hyperliquid annotations, including `category: "preipo"`.
No manual alias list or cross-request market/balance cache is maintained.

Account responses distinguish available-after-maintenance, held, total and unheld
balances. Missing availability is null; totals are not spending power. Positions
cover one deployment. Use account-specific market details for trading capacity.

Transfers support spot, default perps and builder deployments within the same wallet.
Account-mode changes support Unified and Standard. Both require owner keys without
account/vault overrides, as do deposits and withdrawals. Optional
`X-Hyperliquid-Account-Address` / `X-Hyperliquid-Vault-Address` headers select existing
exchange-authorized trading contexts; `userAddress` is a read-only subject selector.

Writes can have partial or unknown outcomes. Inspect exchange results and reconcile
with fresh reads; never repeat a write to poll. Legacy bridge deposits require at
least 5 USDC and Arbitrum ETH gas. Arbitrum confirmation is not Hyperliquid credit;
withdrawal submission is not destination receipt. CCTP is not implemented.

## Develop

```sh
uv sync --frozen
uv run hyperliquid-mcp
uv run pytest -q
uv run ruff check --no-cache .
uv run ruff format --check --no-cache .
```

Python 3.11+. `pyproject.toml` declares dependencies; `uv.lock` locks them.
The local default is testnet at `http://127.0.0.1:8001/mcp`. `.env.example` documents
settings; export them in your shell (the server does not load a .env automatically).
Public deployments require HTTPS. Network selection is server-wide.

Tests block exchange network calls. Wire tests exercise real SDK signing with
intercepted HTTP. Mocked transaction tests do not prove live settlement. Live checks
should use unfunded wallets and read-only operations unless funds are authorized.

## Layout

- `src/hosted_hyperliquid/`: server/auth, tool schemas and instructions, domain operations, upstream validation and SDK session cleanup.
- `src/hosted_hyperliquid/bridge.py`: deposits and withdrawals; `account_reads.py` includes account-specific trading capacity.
- `tests/`: behavior and integration tests, including uncertainty and credential handling.
- `assets/`: public connector metadata and the 512×512 PNG icon.

## Running your own instance

Use HTTPS for public access and keep credentials out of logs. Run one application
process: signer coordination is in-process. Multiple workers require shared signer
coordination to prevent nonce collisions. Install runtime dependencies with
`uv sync --frozen --no-dev` and configure the variables in `.env.example`.

`assets/connector.json` describes the Tendle-hosted connector; `assets/icon.png` is
its 512×512 public icon. Deployment infrastructure is maintained separately.

## Attribution

Adapted from [edkdev/hyperliquid-mcp](https://github.com/edkdev/hyperliquid-mcp), commit
`7f3965182f50f28f7a22cbc8f93e5bf0a0439f14`. MIT attribution is retained in LICENSE.

Credentials accept a 64-hex private key (optional `0x`) or a valid English 12/24-word BIP-39 phrase. Normalize phrase whitespace to single spaces in headers. With a phrase, call `hyperliquid_list_wallet_accounts`, let the user choose, then pass its `{index,address}` as `walletAccount` on every other authenticated tool. No default selection or server-side selection state. Discovery pages through `m/44'/60'/0'/0/index` (10 per page), with no extra passphrase; addresses may be unused. Private-key credentials need no selection. Confirm the returned account address; accounts under other derivation paths or passphrases require their private key. Wallet generation returns `address`, `privateKey`, and `seedPhrase` for the same account. Both secrets are visible to the tool caller; the server does not persist them. HD derivation uses the pinned eth-account library’s explicitly unaudited HD-wallet API.
