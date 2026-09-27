from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import pytest
import requests
from eth_account import Account
from starlette.testclient import TestClient

from hosted_hyperliquid import server, tools

BASE = "http://127.0.0.1:8001"


@pytest.fixture(autouse=True)
def no_live_hyperliquid(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must never contact Hyperliquid")

    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture
def wallet():
    return Account.create()


@pytest.fixture
def client():
    with TestClient(server.create_app(public_url=BASE, testnet=True), base_url=BASE) as client:
        yield client


@pytest.fixture
def fake_sdk(monkeypatch):
    info = create_autospec(tools.Info, instance=True)
    info.session = Mock()
    info.meta.return_value = {"universe": [{"name": "ETH", "maxLeverage": 50, "szDecimals": 4}]}
    info.user_state.return_value = {
        "marginSummary": {
            "accountValue": "100",
            "totalMarginUsed": "10",
            "totalNtlPos": "20",
            "totalRawUsd": "90",
        },
        "withdrawable": "90",
        "assetPositions": [],
    }
    info.query_user_abstraction_state.return_value = "disabled"
    info.spot_user_state.return_value = {"balances": []}
    info.open_orders.return_value = [{"coin": "ETH", "oid": 9007199254740993}]
    info.all_mids.return_value = {"ETH": "2000"}
    info.l2_snapshot.return_value = {"levels": [[], []]}
    info.query_order_by_oid.return_value = {"status": "unknownOid"}
    for method in ("user_fills_by_time", "user_funding_history", "funding_history", "candles_snapshot"):
        getattr(info, method).return_value = []
    for method in ("historical_orders", "user_non_funding_ledger_updates", "portfolio"):
        getattr(info, method).return_value = []
    info.post.return_value = {
        "name": "Demo vault",
        "vaultAddress": "0x" + "1" * 40,
        "portfolio": [],
        "followers": [],
    }
    exchange = create_autospec(tools.Exchange, instance=True)
    exchange.session = Mock()
    exchange.info = Mock()
    exchange._slippage_price.return_value = 2100.0
    for method in ("order", "bulk_orders", "cancel", "bulk_cancel", "modify_order"):
        getattr(exchange, method).return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}},
        }
    info_factory, exchange_factory = Mock(return_value=info), Mock(return_value=exchange)
    monkeypatch.setattr(tools, "Info", info_factory)
    monkeypatch.setattr(tools, "Exchange", exchange_factory)
    return SimpleNamespace(
        info=info, exchange=exchange, info_factory=info_factory, exchange_factory=exchange_factory
    )


@pytest.fixture
def trading(fake_sdk):

    def meta(dex=""):
        symbol = dex + ":SP500" if dex else "ETH"
        return {"universe": [{"name": symbol, "szDecimals": 3, "maxLeverage": 25}], "collateralToken": 0}

    fake_sdk.info.meta.side_effect = meta
    fake_sdk.info.spot_meta.return_value = {
        "tokens": [
            {"index": 5, "name": "HYPE", "szDecimals": 2},
            {"index": 0, "name": "USDC", "szDecimals": 8},
        ],
        "universe": [{"index": 1, "name": "@1", "tokens": [5, 0]}],
    }
    fake_sdk.info.all_mids.side_effect = lambda dex="": (
        {dex + ":SP500": "7731.2"} if dex else {"ETH": "2000", "@1": "40"}
    )
    fake_sdk.info.user_state.return_value["assetPositions"] = [
        {"position": {"coin": "xyz:SP500", "szi": "-0.01", "leverage": {"type": "isolated", "value": 10}}}
    ]
    fake_sdk.info.query_order_by_cloid.return_value = {"status": "unknownOid"}
    for method in ("bulk_modify_orders_new", "bulk_cancel_by_cloid"):
        getattr(fake_sdk.exchange, method).return_value = {
            "status": "ok",
            "response": {"data": {"statuses": ["success"]}},
        }
    for method in ("update_leverage", "update_isolated_margin", "schedule_cancel"):
        getattr(fake_sdk.exchange, method).return_value = {"status": "ok", "response": {"type": "default"}}
    return fake_sdk
