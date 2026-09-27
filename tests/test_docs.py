import json

import pytest
from test_server import call

from hosted_hyperliquid.catalog import CATALOG
from hosted_hyperliquid.docs import GROUPS, build_docs


def test_public_docs_match_http_exactly(client, fake_sdk):
    result = call(client, None, "hyperliquid_get_docs")
    assert not result.get("isError", False)
    text = result["content"][0]["text"]
    response = client.get("/mcp/docs")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.text == text
    assert "Network: testnet" in text
    assert call(client, None, "hyperliquid_get_docs", {"unexpected": True})["isError"]
    assert call(client, None, "hyperliquid_get_instructions")["isError"]
    fake_sdk.info_factory.assert_not_called()
    fake_sdk.exchange_factory.assert_not_called()


@pytest.mark.parametrize("network", ["testnet", "mainnet"])
def test_docs_network_and_url(network):
    text = build_docs(network, "https://example.com")
    assert f"Network: {network}" in text
    assert "https://example.com/connectors/hyperliquid/mcp/docs" in text


def test_reference_covers_each_tool_once_and_preserves_schema_rules():
    names = [name for group in GROUPS.values() for name in group]
    assert len(names) == len(set(names)) == len(CATALOG)
    assert set(names) == {t["name"].removeprefix("hyperliquid_") for t in CATALOG}
    text = build_docs("testnet", "https://example.com")
    for tool in CATALOG:
        heading = "#### " + tool["name"] + "\n"
        assert text.count(heading) == 1
        section = text.split(heading)[1].split("#### ")[0]
        raw = section.split("```json\n")[1].split("\n```")[0]
        rendered = json.loads(raw)
        expected = dict(tool["inputSchema"])
        expected["properties"] = {k: v for k, v in expected["properties"].items() if k != "walletAccount"}
        assert rendered == expected


def test_initialization_points_to_docs(client):
    result = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "docs-test", "version": "1"},
            },
        },
    ).json()["result"]
    assert "hyperliquid_get_docs" in result["instructions"]
    assert "Configured network: testnet" in result["instructions"]
    assert len(result["instructions"]) < 500
