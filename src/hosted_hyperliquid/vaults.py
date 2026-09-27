"""Read-only discovery from Hyperliquid's public stats service."""

import math

import requests
from eth_utils import is_address
from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL

from .read_validation import decimal_string


def list_vaults(base_url, arguments, timeout=10):
    network = {MAINNET_API_URL: "Mainnet", TESTNET_API_URL: "Testnet"}[base_url]
    url = f"https://stats-data.hyperliquid.xyz/{network}/vaults"
    with requests.Session() as session:
        response = session.get(url, timeout=timeout, allow_redirects=False)
        response.raise_for_status()
        entries = response.json()
    if not isinstance(entries, list):
        raise TypeError("Unexpected vault directory response")
    query = arguments.get("search", "").strip().casefold()
    vaults = []
    for entry in entries:
        summary = entry["summary"]
        if (
            not isinstance(summary.get("name"), str)
            or not is_address(summary.get("vaultAddress"))
            or not is_address(summary.get("leader"))
            or type(summary.get("isClosed")) is not bool
        ):
            raise ValueError("Malformed vault summary")
        apr = entry.get("apr")
        if apr is not None and (type(apr) not in (int, float) or not math.isfinite(apr)):
            raise ValueError("Malformed vault APR")
        if not arguments.get("includeClosed", False) and summary["isClosed"]:
            continue
        if query and not any(
            query in summary[field].casefold() for field in ("name", "vaultAddress", "leader")
        ):
            continue
        tvl = decimal_string(summary["tvl"])
        vaults.append((tvl, {**summary, "apr": apr}))
    vaults.sort(key=lambda item: (item[0].copy_negate(), item[1]["vaultAddress"]))
    offset, limit = arguments.get("offset", 0), arguments.get("limit", 20)
    page = [item[1] for item in vaults[offset : offset + limit]]
    return {
        "message": "Vault directory retrieved. APR is historical; use vault_details for current vault information.",
        "data": {
            "vaults": page,
            "total": len(vaults),
            "offset": offset,
            "limit": limit,
            "nextOffset": offset + len(page) if offset + len(page) < len(vaults) else None,
            "network": network.lower(),
            "source": url,
            "sort": "tvl_desc",
        },
    }
