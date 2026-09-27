"""Market discovery contracts, exact identity, and authenticated MCP integration."""

import copy
import json
from unittest.mock import Mock

import pytest
from test_server import call

from hosted_hyperliquid import markets


@pytest.fixture
def feed(monkeypatch):
    token = lambda index, name: {"index": index, "name": name, "szDecimals": 3}
    asset = lambda name, **kw: {"name": name, "szDecimals": 3, "maxLeverage": 25, "marginTableId": 25, **kw}
    meta = lambda *assets: {
        "universe": list(assets),
        "collateralToken": 7,
        "marginTables": [[25, {"marginTiers": [{"lowerBound": "0", "maxLeverage": 25}]}]],
    }
    default = meta(asset("BTC"), asset("OLD", isDelisted=True))
    builder = meta(asset("mkts:US500"))
    spot = {
        "tokens": [token(11, "ABC"), token(7, "USDC"), token(20, "ABC")],
        "universe": [
            {"name": "@1", "tokens": [11, 7], "index": 1},
            {"name": "@4", "tokens": [20, 7], "index": 4},
        ],
    }
    data = {
        ("perpConciseAnnotations", ""): [
            ["mkts:US500", {"displayName": "S&P500", "category": "indices", "keywords": ["S&P 500", "SPY"]}]
        ],
        ("tokenConciseAnnotations", ""): [
            [11, {"displayName": "Alpha", "category": "crypto", "keywords": ["Alphabet"]}]
        ],
        ("perpDexs", ""): [None, {"name": "mkts", "fullName": "Markets By Kinetiq"}],
        ("allPerpMetas", ""): [default, builder],
        ("meta", ""): default,
        ("meta", "mkts"): builder,
        ("spotMeta", ""): spot,
        ("metaAndAssetCtxs", ""): [default, [{"markPx": "12.345", "midPx": None}, {"markPx": "0"}]],
        ("metaAndAssetCtxs", "mkts"): [builder, [{"markPx": "770.96", "oraclePx": "771.1"}]],
        ("spotMetaAndAssetCtxs", ""): [
            spot,
            [{"coin": "@999", "markPx": "999"}, {"coin": "@4", "markPx": "4"}, {"coin": "@1", "markPx": "1"}],
        ],
    }
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)

    def post(url, **kwargs):
        assert url.endswith("/info")
        assert kwargs["timeout"] == 10
        assert kwargs["allow_redirects"] is False
        assert "headers" not in kwargs
        payload = kwargs["json"]
        return Mock(
            status_code=200,
            json=Mock(return_value=copy.deepcopy(data[(payload["type"], payload.get("dex", ""))])),
        )

    session.post.side_effect = post
    monkeypatch.setattr(markets.requests, "Session", Mock(return_value=session))
    return data, session


def run(client, wallet, tool, args):
    result = call(client, wallet, "hyperliquid_" + tool, args)
    assert not result.get("isError", False), result
    return json.loads(result["content"][0]["text"])


def test_search_all_deployments_and_spot(client, wallet, feed, fake_sdk):
    result = run(client, wallet, "list_markets", {})
    assert result["total"] == 4
    assert {r["marketId"] for r in result["markets"]} == {"perp:BTC", "perp:mkts:US500", "spot:@1", "spot:@4"}
    assert feed[1].post.call_count == 5
    fake_sdk.info_factory.assert_not_called()
    fake_sdk.exchange_factory.assert_not_called()
    feed[1].__exit__.assert_called_once()
    rows = run(client, wallet, "list_markets", {"search": "kinetiq"})["markets"]
    assert rows[0]["symbol"] == "mkts:US500"
    assert rows[0]["deploymentName"] == "Markets By Kinetiq"
    assert rows[0]["supportedByOrderTools"]


def test_filters_and_pagination(client, wallet, feed):
    result = run(client, wallet, "list_markets", {"dex": "", "includeDelisted": True, "limit": 1})
    assert result["total"] == 2 and result["nextOffset"] == 1
    result = run(client, wallet, "list_markets", {"dex": "", "includeDelisted": True, "offset": 1})
    assert result["markets"][0]["status"] == "delisted" and result["nextOffset"] is None
    assert run(client, wallet, "list_markets", {"marketType": "spot", "search": "abc"})["total"] == 2
    assert run(client, wallet, "list_markets", {"search": "missing"})["total"] == 0
    assert run(client, wallet, "list_markets", {"offset": 999})["markets"] == []


@pytest.mark.parametrize(
    "market_id,price",
    [("perp:BTC", "12.345"), ("perp:mkts:US500", "770.96"), ("spot:@1", "1"), ("spot:@4", "4")],
)
def test_details_exact_prices_collateral_precision(client, wallet, feed, market_id, price):
    result = run(client, wallet, "market_details", {"marketId": market_id})
    assert result["prices"]["markPx"] == price
    assert result["tradingEnabled"] is None
    assert result["precision"]["sizeDecimals"] == 3
    if market_id.startswith("perp:"):
        assert result["collateral"]["name"] == "USDC"
        assert result["maxLeverage"] == 25
        assert result["marginTable"]["marginTiers"][0]["maxLeverage"] == 25
        assert result["precision"]["maxPriceDecimals"] == 3
    else:
        assert result["quoteToken"]["name"] == "USDC"
        assert result["collateral"] is None and result["maxLeverage"] is None
        assert result["precision"]["maxPriceDecimals"] == 5


def test_delisted(client, wallet, feed):
    result = run(client, wallet, "market_details", {"marketId": "perp:OLD"})
    assert result["tradingEnabled"] is False and not result["supportedByOrderTools"]


@pytest.mark.parametrize(
    "tool,args",
    [
        ("list_markets", {"dex": "missing"}),
        ("list_markets", {"dex": "", "marketType": "spot"}),
        ("market_details", {"marketId": "perp:btc"}),
        ("market_details", {"marketId": "perp:unknown:X"}),
        ("market_details", {"marketId": "spot:ABC/USDC"}),
    ],
)
def test_unknown_or_ambiguous_not_guessed(client, wallet, feed, tool, args):
    assert call(client, wallet, "hyperliquid_" + tool, args)["isError"]


@pytest.mark.parametrize(
    "args",
    [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"includeDelisted": "yes"}, {"marketType": "options"}],
)
def test_invalid_schema_no_request(client, wallet, feed, args):
    assert call(client, wallet, "hyperliquid_list_markets", args)["isError"]
    feed[1].post.assert_not_called()


@pytest.mark.parametrize("case", ["mapping", "count", "tokens", "contexts", "decimal", "duplicate_context"])
def test_malformed_upstream_is_error(client, wallet, feed, case):
    data, _ = feed
    tool, args = "list_markets", {}
    if case == "mapping":
        data[("allPerpMetas", "")].reverse()
    if case == "count":
        data[("allPerpMetas", "")].pop()
    if case == "tokens":
        data[("spotMeta", "")]["tokens"].pop()
    if case == "contexts":
        data[("metaAndAssetCtxs", "")][1].pop()
        tool, args = "market_details", {"marketId": "perp:BTC"}
    if case == "decimal":
        data[("metaAndAssetCtxs", "")][1][0]["markPx"] = "NaN"
        tool, args = "market_details", {"marketId": "perp:BTC"}
    if case == "duplicate_context":
        data[("spotMetaAndAssetCtxs", "")][1].append({"coin": "@1", "markPx": "2"})
        tool, args = "market_details", {"marketId": "spot:@1"}
    assert call(client, wallet, "hyperliquid_" + tool, args)["isError"]


@pytest.mark.parametrize("tool,args", [("list_markets", {}), ("market_details", {"marketId": "perp:BTC"})])
@pytest.mark.parametrize("failure", ["timeout", "redirect", "rate_limit", "json"])
def test_failure_redacted_no_retry_session_closed(client, wallet, feed, tool, args, failure, caplog):
    _, session = feed
    secret = wallet.key.hex()
    if failure == "timeout":
        session.post.side_effect = TimeoutError(secret)
    else:
        session.post.side_effect = None
        session.post.return_value = Mock(
            status_code={"redirect": 302, "rate_limit": 429, "json": 200}[failure],
            json=Mock(side_effect=ValueError(secret)),
        )
    result = call(client, wallet, "hyperliquid_" + tool, args)
    assert result["isError"] and secret not in json.dumps(result) + caplog.text
    session.post.assert_called_once()
    session.__exit__.assert_called_once()


def test_builder_symbols_can_contain_colons_and_spaces(client, wallet, feed):
    data, _ = feed
    symbol = "mkts:A B:C"
    data[("allPerpMetas", "")][1]["universe"][0]["name"] = symbol
    row = run(client, wallet, "list_markets", {"search": "A B:C"})["markets"][0]
    result = run(client, wallet, "market_details", {"marketId": row["marketId"]})
    assert result["symbol"] == symbol and result["dex"] == "mkts"


def test_ui_keyword_category_and_details_share_identity(client, wallet, feed):
    result = run(client, wallet, "list_markets", {"search": "spy", "category": "INDICES"})
    assert result["total"] == 1
    row = result["markets"][0]
    assert row["marketId"] == "perp:mkts:US500" and row["symbol"] == "mkts:US500"
    assert row["displayName"] == "S&P500" and row["category"] == "indices"
    detail = run(client, wallet, "market_details", {"marketId": row["marketId"]})
    for field in ("marketId", "symbol", "displayName", "category", "keywords"):
        assert detail[field] == row[field]
    assert "indices" in result["categories"]
    assert run(client, wallet, "list_markets", {"category": "preipo"})["total"] == 0


def test_company_search_across_builders_and_delisted(client, wallet, feed):
    data, _ = feed
    builder = data[("allPerpMetas", "")][1]
    builder["universe"].extend(
        [
            {"name": "mkts:ANTH", "szDecimals": 3, "maxLeverage": 6},
            {"name": "mkts:OAI", "szDecimals": 3, "maxLeverage": 6},
            {"name": "mkts:OLDANTH", "szDecimals": 3, "maxLeverage": 3, "isDelisted": True},
        ]
    )
    data[("perpConciseAnnotations", "")].extend(
        [
            ["mkts:ANTH", {"keywords": ["anthropic"], "category": "preipo"}],
            ["mkts:OAI", {"displayName": "OPENAI", "keywords": ["openai", "oai"], "category": "preipo"}],
            ["mkts:OLDANTH", {"keywords": ["anthropic"], "category": "preipo"}],
        ]
    )
    assert run(client, wallet, "list_markets", {"search": "Anthropic"})["total"] == 1
    assert run(client, wallet, "list_markets", {"search": "Anthropic", "includeDelisted": True})["total"] == 2
    assert (
        run(client, wallet, "list_markets", {"search": "OpenAI"})["markets"][0]["marketId"] == "perp:mkts:OAI"
    )
    page = run(client, wallet, "list_markets", {"category": "preipo", "limit": 1})
    assert page["total"] == 2 and page["nextOffset"] == 1
    assert run(client, wallet, "list_markets", {"category": "preipo", "offset": 1})["nextOffset"] is None


def test_spot_keywords_full_names_and_null_category(client, wallet, feed):
    data, session = feed
    data[("spotMeta", "")]["tokens"][2]["fullName"] = "Another Company"
    result = run(client, wallet, "list_markets", {"marketType": "spot", "search": "alphabet"})
    assert result["markets"][0]["displayName"] == "Alpha/USDC"
    assert result["markets"][0]["marketId"] == "spot:@1"
    assert session.post.call_count == 2
    row = run(client, wallet, "list_markets", {"marketType": "spot", "search": "Another Company"})["markets"][
        0
    ]
    assert row["marketId"] == "spot:@4" and row["category"] is None
    assert run(client, wallet, "list_markets", {"marketType": "spot", "category": "crypto"})["total"] == 1
    detail = run(client, wallet, "market_details", {"marketId": "spot:@1"})
    assert detail["displayName"] == "Alpha/USDC" and "Alphabet" in detail["keywords"]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        [["BTC", {"keywords": "bad"}]],
        [["BTC", {"category": 3}]],
        [["BTC", {}], ["BTC", {}]],
        [[3, {}]],
    ],
)
def test_invalid_annotations_are_not_empty_search(client, wallet, feed, payload):
    data, _ = feed
    data[("perpConciseAnnotations", "")] = payload
    result = call(client, wallet, "hyperliquid_list_markets", {"search": "Anthropic"})
    assert result["isError"] and "Search coverage is unknown" in str(result)


def test_annotation_failure_preserves_redaction_and_closes_session(client, wallet, feed):
    _, session = feed
    original = session.post.side_effect

    def post(url, **kwargs):
        if kwargs["json"]["type"] == "perpConciseAnnotations":
            raise TimeoutError(wallet.key.hex())
        return original(url, **kwargs)

    session.post.side_effect = post
    result = call(client, wallet, "hyperliquid_market_details", {"marketId": "perp:BTC"})
    assert result["isError"] and "annotations unavailable" in str(result)
    assert wallet.key.hex() not in str(result)
    session.__exit__.assert_called_once()


def test_annotation_cannot_override_execution_fields(client, wallet, feed):
    data, _ = feed
    data[("perpConciseAnnotations", "")][0][1].update(
        symbol="wrong", marketId="perp:WRONG", supportedByOrderTools=False
    )
    row = run(client, wallet, "list_markets", {"search": "spy"})["markets"][0]
    assert row["symbol"] == "mkts:US500" and row["supportedByOrderTools"]
