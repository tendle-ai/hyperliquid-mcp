<p align="center">
  <img src="assets/icon.png" width="80" height="80" alt="Hyperliquid" />
</p>

<h1 align="center">Hyperliquid MCP</h1>
<p align="center">Markets, balances, and trading for your AI agent.</p>
<p align="center">
  <a href="https://hyperliquid.tendle.ai/mcp/docs">Docs</a> ·
  <a href="https://tendle.ai/connectors/hyperliquid/manifest.json">Manifest</a> ·
  <a href="https://tendle.ai/connectors/hyperliquid">Connector page</a> ·
  <a href="https://tendle.ai">Built by Tendle</a>
</p>

> [!TIP]
> **Agent quickstart** · Paste this into your agent:
>
> Add Hyperliquid from https://hyperliquid.tendle.ai

| Connection | |
| :--- | :--- |
| MCP endpoint | `https://hyperliquid.tendle.ai/mcp` |
| Transport | Streamable HTTP |
| Hosted network | **Mainnet** |
| Authentication | Wallet private key or seed phrase, stored in your client's secure credentials |

Name the connector **Hyperliquid**. If your client needs a provider ID, use
`hyperliquid`. Have the agent read `hyperliquid_get_docs` before using tools.

## Try asking

- “Find Anthropic and OpenAI markets. Show their collateral and leverage limits.”
- “How much of my balance is available, and how much is held?”
- “Show my positions and open orders on the xyz deployment.”
- “Check my trading capacity before placing an order.”

## What it covers

Discover and trade default perpetuals, builder/HIP-3 perpetuals, and spot markets.
Search uses live market names and upstream keywords, including pre-IPO categories.

| Workflow | Tools |
| :--- | :--- |
| Wallet setup | `generate_wallet`, `list_wallet_accounts` |
| Markets | `list_markets`, `market_details`, `get_order_book`, `get_historical_funding`, `get_candles` |
| Account | `get_account`, `get_orders`, `get_order_status`, `get_user_fills`, `get_user_funding`, `get_ledger_updates`, `get_portfolio` |
| Orders | `place_orders`, `modify_orders`, `cancel_orders`, `cancel_all_orders`, `place_bracket_order`, `close_position`, `schedule_cancel` |
| Margin & collateral | `update_leverage`, `update_isolated_margin`, `set_account_mode`, `transfer_collateral` |
| Bridge | `deposit`, `withdraw` |
| Vaults | `list_vaults`, `vault_details` |
| Reference | `get_docs` |

All 30 tool names begin with `hyperliquid_`. [Live docs](https://hyperliquid.tendle.ai/mcp/docs)
contain the current inputs and behavior; `tools/list` supplies the schemas.

## Connect a wallet

Initialization, tool discovery, `hyperliquid_get_docs`, and
`hyperliquid_generate_wallet` work without credentials. Other tools require
`Authorization: Bearer <credential>`. Use the client's secure credential settings,
not prompts or URLs. An invalid supplied credential is rejected even on public tools.

- **Private key:** 64 hexadecimal characters, optionally prefixed with `0x`.
- **Seed phrase:** a valid English 12- or 24-word BIP-39 phrase. The agent lists
  accounts with `hyperliquid_list_wallet_accounts`, asks you to choose, and passes
  the selected `{index,address}` as `walletAccount` on subsequent authenticated calls.

Keys are request-scoped and are not persisted by the server. Wallet generation
returns an address, private key, and seed phrase. **Both secrets are visible to the
tool caller** and must be saved securely by the client.

<details>
<summary>Account selection and advanced credentials</summary>

Phrase discovery uses `m/44'/60'/0'/0/index`, ten accounts per page, with no extra
passphrase. Addresses may be unused. Other derivation paths or passphrases require
an individual private key. Normalize phrase whitespace to single spaces in headers.
The pinned eth-account library marks its HD-wallet API as unaudited.

`X-Hyperliquid-Account-Address` and `X-Hyperliquid-Vault-Address` select existing
exchange-authorized contexts. `userAddress` selects the subject of a read, not a signer.
Collateral transfers, account-mode changes, deposits, and withdrawals require
owner keys without account/vault overrides.

Clients migrating from the old Tendle endpoint must update their credential host
as well as their MCP URL. Muse requires secure credential re-entry for that change.

</details>

## Before trading

- **Query fresh state.** Total balance is not available collateral. Account responses
  separate held, unheld, and available-after-maintenance amounts; unknown availability
  is `null`. Positions cover the selected deployment.
- **Use the discovered market ID.** Check `market_details` with account information
  for that market's trading capacity. Similar symbols can identify different assets.
- **Verify writes.** Partial or uncertain outcomes need fresh reads, not a repeated
  order. A submitted withdrawal is not proof of receipt.
- **Bridge requirements:** legacy deposits need at least 5 USDC and Arbitrum ETH for
  gas. Arbitrum confirmation does not prove Hyperliquid credit. CCTP is not implemented.

## Run locally

Requires **Python 3.11+** and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --frozen
uv run hyperliquid-mcp
```

The local default is **testnet**, at `http://127.0.0.1:8001/mcp`.
See [.env.example](.env.example) for settings and export them in your shell;
the application does not automatically load `.env`. Network selection is server-wide.

```sh
uv run pytest -q
uv run ruff check --no-cache .
uv run ruff format --check --no-cache .
```

Tests intercept exchange HTTP calls, including real SDK signing. They do not prove
live transaction settlement. Muse installation and authenticated reads have also
been verified against the hosted service.

<details>
<summary>Self-hosting and repository layout</summary>

Use HTTPS, keep credentials out of logs, and install runtime dependencies with
`uv sync --frozen --no-dev`. Run one application process: signer coordination is
in-process, so multiple workers need shared coordination to prevent nonce collisions.

| Path | Contents |
| :--- | :--- |
| `src/hosted_hyperliquid/` | MCP server, authentication, tool schemas, and exchange operations |
| `tests/` | Behavior and integration tests |
| `assets/` | Connector manifest and icon |
| `pyproject.toml` / `uv.lock` | Package configuration and locked dependencies |

The manifest describes the Tendle-hosted service. Update its URLs for your own
instance. Deployment infrastructure is maintained separately.

</details>

## Credits

Adapted from [edkdev/hyperliquid-mcp](https://github.com/edkdev/hyperliquid-mcp)
at commit `7f3965182f50f28f7a22cbc8f93e5bf0a0439f14`.
[MIT license](LICENSE). [Support](mailto:hello@tendle.ai).
