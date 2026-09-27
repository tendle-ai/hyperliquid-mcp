"""Stateless HTTP MCP entry point; public TLS is terminated by the reverse proxy."""

import json
import logging
import os
import threading
import time
from urllib.parse import urlsplit

import jsonschema
import uvicorn
from hyperliquid.utils import constants
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from .auth import (
    WalletAuthMiddleware,
    WalletSelectionError,
    current_wallet,
    list_wallet_accounts,
    select_wallet,
)
from .bridge import DepositError, WithdrawalError
from .catalog import CATALOG, WRITE_TOOLS
from .docs import PUBLIC_TOOLS, VERSION, build_docs
from .read_validation import ReadInputError
from .tools import (
    HyperliquidTools,
    generate_wallet,
)
from .trading import TradingInputError

logger = logging.getLogger(__name__)

TOOLS = {item["name"]: Tool(**item) for item in CATALOG}
VALIDATORS = {name: jsonschema.Draft202012Validator(tool.input_schema) for name, tool in TOOLS.items()}
# Bounded lock stripes use public signer addresses, never private keys. One worker
# serializes writes per signer to avoid the SDK's millisecond nonce collisions.
SIGNER_LOCKS = [threading.Lock() for _ in range(64)]
LAST_WRITE_MS = [0 for _ in range(64)]


def result_error(message: str) -> CallToolResult:
    return CallToolResult(is_error=True, content=[TextContent(type="text", text=message)])


def argument_error(error):
    # Use only schema-owned names/rules, never jsonschema's value-echoing message.
    schema_path = list(error.absolute_schema_path)
    fields = [schema_path[i + 1] for i, part in enumerate(schema_path[:-1]) if part == "properties"]
    location = ".".join(fields) or "arguments"
    rule = error.validator
    if rule == "required":
        missing = [key for key in error.validator_value if key not in error.instance]
        detail = "missing required field(s): " + ", ".join(missing)
    elif rule in {
        "type",
        "enum",
        "const",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "minItems",
        "maxItems",
        "maxLength",
    }:
        detail = str(rule) + " must match " + json.dumps(error.validator_value)
    elif rule == "additionalProperties":
        detail = "contains unsupported fields; use only the listed schema properties"
    elif rule == "pattern":
        detail = "format must match " + error.validator_value
    else:
        detail = "invalid field combination; check required, mutually exclusive and order-type fields"
    return f"Invalid arguments at {location}: {detail}. No action submitted."


def exchange_rejected(result: dict) -> bool:
    data = result.get("data", {})
    if not isinstance(data, dict):
        return False
    if data.get("status") == "err" or "error" in data:
        return True
    response = data.get("response", {})
    if not isinstance(response, dict):
        return False
    detail = response.get("data")
    statuses = detail.get("statuses", []) if isinstance(detail, dict) else []
    return isinstance(statuses, list) and any(
        isinstance(status, dict) and "error" in status for status in statuses
    )


def execute_tool(identity, base_url, name, arguments):
    if name == "hyperliquid_generate_wallet":
        return generate_wallet()
    if identity is None:
        raise ValueError("Authenticated wallet required")
    if name == "hyperliquid_list_wallet_accounts":
        return list_wallet_accounts(identity, arguments)
    arguments = dict(arguments)
    selection = arguments.pop("walletAccount", None)
    identity = select_wallet(identity, selection)
    handler = HyperliquidTools(identity, base_url)
    try:
        if name in WRITE_TOOLS:
            stripe = int(identity.wallet.address, 16) % len(SIGNER_LOCKS)
            with SIGNER_LOCKS[stripe]:
                delay_ms = LAST_WRITE_MS[stripe] + 1 - time.time_ns() // 1_000_000
                if delay_ms > 1000:
                    raise RuntimeError("Clock moved backwards")
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000)
                try:
                    return handler.execute(name, arguments)
                finally:
                    LAST_WRITE_MS[stripe] = time.time_ns() // 1_000_000
        return handler.execute(name, arguments)
    finally:
        handler.close()


def create_app(*, public_url: str | None = None, testnet: bool | None = None):
    public_url = public_url or os.getenv("MCP_PUBLIC_URL", "http://127.0.0.1:8001")
    parsed = urlsplit(public_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError(
            "MCP_PUBLIC_URL must be an HTTP(S) origin without credentials, path, query or fragment"
        )
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Public deployments require an HTTPS MCP_PUBLIC_URL")
    if testnet is None:
        value = os.getenv("HYPERLIQUID_TESTNET", "true").lower()
        if value not in {"true", "false"}:
            raise ValueError("HYPERLIQUID_TESTNET must be true or false")
        testnet = value == "true"
    base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL

    network = "testnet" if testnet else "mainnet"
    documentation = build_docs(network, public_url)

    async def list_tools(ctx, params):
        return ListToolsResult(tools=list(TOOLS.values()))

    async def call_tool(ctx, params):
        name, arguments = params.name, params.arguments or {}
        if name not in TOOLS:
            return result_error(
                "Unknown tool. Refresh tools/list and use a current tool name; no action submitted."
            )
        identity = current_wallet.get()
        if identity is None and name not in PUBLIC_TOOLS:
            return result_error(
                "Authentication required: configure Authorization: Bearer <private-key-or-seed-phrase> in your client's secure credential settings."
            )
        try:
            VALIDATORS[name].validate(arguments)
        except jsonschema.ValidationError as exc:
            return result_error(argument_error(exc))
        if name == "hyperliquid_get_docs":
            return CallToolResult(content=[TextContent(type="text", text=documentation)])
        # JSON Schema accepts 1.0 as an integer. Give the SDK an actual int,
        # without converting already-integral IDs through a lossy float.
        arguments = dict(arguments)
        for field, schema in TOOLS[name].input_schema.get("properties", {}).items():
            if schema.get("type") == "integer" and field in arguments:
                arguments[field] = int(arguments[field])
        try:
            result = await run_in_threadpool(execute_tool, identity, base_url, name, arguments)
            return CallToolResult(
                is_error=exchange_rejected(result),
                content=[TextContent(type="text", text=json.dumps(result))],
            )
        except (
            DepositError,
            WithdrawalError,
            ReadInputError,
            TradingInputError,
            WalletSelectionError,
        ) as exc:
            return result_error(str(exc))
        except Exception:  # noqa: BLE001 -- redact all SDK exceptions at the credential boundary
            # Never include exception text, headers, request arguments, or traceback.
            logger.warning("tool_execution_failed tool=%s", name)
            if name == "hyperliquid_generate_wallet":
                return result_error(
                    "Wallet generation failed; no wallet credentials were returned. Retry to generate a new wallet."
                )
            if name not in WRITE_TOOLS:
                return result_error(
                    "Read failed or returned invalid data. No result is available; this does not mean zero balance, no positions, or no market. Retry the read; do not substitute stale data."
                )
            return result_error(
                "Tool execution failed; submission status is unknown. Check fresh account state and relevant orders/fills or ledger/chain history before retrying. Do not infer the cause or repeat the write to poll."
            )

    async def health(request):
        return JSONResponse({"status": "ok", "network": "testnet" if testnet else "mainnet"})

    async def docs(request):
        return PlainTextResponse(documentation)

    server = Server(
        "hyperliquid-mcp",
        version=VERSION,
        instructions=f"Configured network: {network}. Call hyperliquid_get_docs before use and refresh tools/list after updates. Query fresh data; respect user authorization. Seed credentials require explicit walletAccount selection; call hyperliquid_list_wallet_accounts and ask the user to choose.",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        max_request_body_size=64 * 1024,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[parsed.netloc],
            allowed_origins=[f"{parsed.scheme}://{parsed.netloc}"],
        ),
        custom_starlette_routes=[Route("/healthz", health), Route("/mcp/docs", docs)],
    )
    app.add_middleware(WalletAuthMiddleware)
    return app


def main():
    uvicorn.run(
        create_app(),
        host=os.getenv("MCP_HOST", "127.0.0.1"),
        port=int(os.getenv("MCP_PORT", "8001")),
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
