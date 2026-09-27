"""Minimal response contracts for read tools; invalid data is not an empty result."""

import re
from decimal import Decimal


def decimal_string(value):
    if not isinstance(value, str) or len(value) > 256 or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("Invalid decimal string")
    return Decimal(value)


def balance_values(state):
    margin = state["marginSummary"]
    for key in ("accountValue", "totalMarginUsed", "totalRawUsd", "totalNtlPos"):
        decimal_string(margin[key])
    decimal_string(state["withdrawable"])


def account_state(state):
    balance_values(state)
    positions = state["assetPositions"]
    if not isinstance(positions, list):
        raise TypeError("Invalid positions")
    for row in positions:
        if not isinstance(row, dict) or not isinstance(row.get("position"), dict):
            raise TypeError("Invalid position")
        if not isinstance(row["position"].get("coin"), str):
            raise TypeError("Invalid position asset")
    cross = state.get("crossMarginSummary")
    if cross is not None:
        balance_values({"marginSummary": cross, "withdrawable": state["withdrawable"]})


def records(value, required=()):
    if not isinstance(value, list):
        raise TypeError("Expected a list")
    for row in value:
        if not isinstance(row, dict) or "error" in row or any(key not in row for key in required):
            raise ValueError("Invalid record")
    return value


def order_status(value):
    if not isinstance(value, dict):
        raise TypeError("Invalid order response")
    if value.get("status") == "unknownOid":
        return
    if value.get("status") != "order" or not isinstance(value.get("order"), dict):
        raise ValueError("Invalid order status")
    order = value["order"]
    if not isinstance(order.get("status"), str) or not isinstance(order.get("order"), dict):
        raise TypeError("Invalid order detail")


class ReadInputError(Exception):
    """Static, safe read-input errors."""


def metadata(value):
    if not isinstance(value, dict):
        raise TypeError("Invalid metadata")
    records(value["universe"], ("name", "maxLeverage", "szDecimals"))
    for asset in value["universe"]:
        if not isinstance(asset["name"], str) or not asset["name"]:
            raise ValueError("Invalid asset name")
        if type(asset["maxLeverage"]) is not int or type(asset["szDecimals"]) is not int:
            raise TypeError("Invalid asset precision or leverage")


def order_book(value):
    if not isinstance(value, dict) or not isinstance(value.get("levels"), list):
        raise TypeError("Invalid order book")
    if len(value["levels"]) != 2:
        raise ValueError("Order book must contain bids and asks")
    for side in value["levels"]:
        records(side, ("px", "sz", "n"))
        for level in side:
            if decimal_string(level["px"]) < 0 or decimal_string(level["sz"]) < 0:
                raise ValueError("Invalid level price or size")
            if type(level["n"]) is not int or level["n"] < 0:
                raise ValueError("Invalid level count")


def market_history(value, candles=False):
    numeric = ("o", "h", "l", "c", "v") if candles else ("fundingRate", "premium")
    required = ("t", "T", "s", "i", "n", *numeric) if candles else ("time", "coin", *numeric)
    records(value, required)
    for row in value:
        for key in numeric:
            decimal_string(row[key])
        for key in ("t", "T", "n") if candles else ("time",):
            if type(row[key]) is not int or row[key] < 0:
                raise ValueError("Invalid history timestamp or count")
        if not isinstance(row["s" if candles else "coin"], str):
            raise TypeError("Invalid history asset")


def vault_detail(value, address):
    if not isinstance(value, dict) or "error" in value:
        raise TypeError("Invalid vault response")
    if not isinstance(value.get("name"), str) or not isinstance(value.get("vaultAddress"), str):
        raise TypeError("Invalid vault identity")
    if value["vaultAddress"].lower() != address.lower():
        raise ValueError("Vault address mismatch")
    if not isinstance(value.get("portfolio"), list):
        raise TypeError("Invalid vault portfolio")
    for period in value["portfolio"]:
        if not isinstance(period, list) or len(period) != 2 or not isinstance(period[1], dict):
            raise ValueError("Invalid vault history")
    records(value["followers"], ("user", "vaultEquity"))


def active_asset(info, address, symbol):
    value = info.post("/info", {"type": "activeAssetData", "user": address, "coin": symbol})
    if value is None:
        return None
    if value["user"].lower() != address.lower() or value["coin"] != symbol:
        raise ValueError("Capacity identity mismatch")
    for field in ("availableToTrade", "maxTradeSzs"):
        if not isinstance(value[field], list) or len(value[field]) != 2:
            raise ValueError("Invalid capacity sides")
        for amount in value[field]:
            if decimal_string(amount) < 0:
                raise ValueError("Invalid capacity amount")
    if decimal_string(value["markPx"]) <= 0:
        raise ValueError("Invalid mark price")
    leverage = value["leverage"]
    if (
        leverage["type"] not in {"cross", "isolated"}
        or type(leverage["value"]) is not int
        or leverage["value"] <= 0
    ):
        raise ValueError("Invalid account leverage")
    return value
