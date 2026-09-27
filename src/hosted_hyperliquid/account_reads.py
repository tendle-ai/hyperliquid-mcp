"""Scoped account reads; never combine incompatible balance accounting modes."""

import time
from decimal import localcontext

from .read_validation import ReadInputError, account_state, active_asset, decimal_string, records

SECTIONS = {
    "role": "user_role",
    "fees": "user_fees",
    "subAccounts": "query_sub_accounts",
    "agents": "extra_agents",
    "multiSigSigners": "query_user_to_multi_sig_signers",
    "referrals": "query_referral_state",
    "rateLimit": "user_rate_limit",
}
MODES = {"unifiedAccount", "portfolioMargin", "disabled", "default", "dexAbstraction"}


def mode(info, address):
    value = info.query_user_abstraction_state(address)
    if not isinstance(value, str) or value not in MODES:
        raise ValueError("Unknown account mode")
    return value


def spot_state(info, address):
    state = info.spot_user_state(address)
    balances = records(state["balances"], ("coin", "token", "total", "hold"))
    available = {}
    pairs = state.get("tokenToAvailableAfterMaintenance", [])
    if not isinstance(pairs, list):
        raise TypeError("Invalid maintenance availability")
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2 or type(pair[0]) is not int or pair[0] in available:
            raise ValueError("Invalid maintenance availability")
        decimal_string(pair[1])
        available[pair[0]] = pair[1]
    rows, seen = [], set()
    for balance in balances:
        token = balance["token"]
        if type(token) is not int or token < 0 or token in seen or not isinstance(balance["coin"], str):
            raise ValueError("Invalid balance identity")
        seen.add(token)
        total, held = decimal_string(balance["total"]), decimal_string(balance["hold"])
        with localcontext() as ctx:
            ctx.prec = 300
            unheld = format(total - held, "f")
        rows.append(
            {
                "coin": balance["coin"],
                "token": token,
                "availableAfterMaintenance": available.get(token),
                "held": balance["hold"],
                "total": balance["total"],
                "unheld": unheld,
            }
        )
    return {
        "balances": rows,
        "note": "Amounts are token units. Lead with availableAfterMaintenance when reporting availability; this is exchange-reported, null means unavailable. total includes held funds and is not spending power. held is the exchange hold, not an explanation of its cause. unheld = total - held is arithmetic only, not a substitute for maintenance availability or withdrawable funds. Use market_details(includeAccount=true) for a specific perp trade's capacity.",
    }


def read_account(handler, name, args, address):
    info = handler.info
    common = {"userAddress": address, "observedAt": int(time.time() * 1000)}
    if name == "hyperliquid_get_account":
        account_mode = mode(info, address)
        spot = spot_state(info, address)
        dex = args.get("dex", "")
        state = info.user_state(address, dex=dex)
        account_state(state)
        interpretation = (
            "separate"
            if account_mode == "disabled"
            else "shared"
            if account_mode in {"unifiedAccount", "portfolioMargin"}
            else "unverified"
        )
        states = {dex: state}
        if dex:
            states[""] = info.user_state(address, dex="")
            account_state(states[""])
        result = {
            **common,
            "accountMode": account_mode,
            "dex": dex,
            "positions": state["assetPositions"],
            "balanceLocations": {
                "interpretation": interpretation,
                "spot": {"role": "shared_collateral" if interpretation == "shared" else "spot", **spot},
                "perps": [
                    {
                        "dex": deployment,
                        "reportedMarginSummary": data["marginSummary"],
                        "reportedWithdrawable": data["withdrawable"],
                    }
                    for deployment, data in sorted(states.items())
                ],
            },
            "note": "Hyperliquid only; no Arbitrum balances. Positions cover only the selected dex; an empty list does not establish that all deployments are flat. Perp balance entries are unique deployments, not summed totals. In shared mode use spot availableAfterMaintenance, held and total; zero reported perp totals do not mean unfunded perps or prove no activity. Raw default/dexAbstraction are unverified, not confirmed Standard. Cross/isolated position margin is separate from account mode. Account value is not market capacity: use market_details with includeAccount=true.",
        }
        for section in args.get("include", []):
            value = getattr(info, SECTIONS[section])(address)
            if (
                not isinstance(value, (dict, list, type(None)))
                or isinstance(value, dict)
                and "error" in value
            ):
                raise ValueError("Invalid account information")
            result[section] = value
        return result
    if name == "hyperliquid_get_orders":
        if args["view"] == "open":
            data = info.open_orders(address, dex=args.get("dex", ""))
            records(data, ("coin", "oid"))
        else:
            data = info.historical_orders(address)
            records(data, ("order", "status", "statusTimestamp"))
        return {
            **common,
            "data": data,
            "view": args["view"],
            "completeHistory": False,
            "note": "Open orders cover the selected perp deployment. Historical orders are API-bounded; absence is not proof an order never existed.",
        }
    if name == "hyperliquid_get_ledger_updates":
        data = info.user_non_funding_ledger_updates(address, args["startTime"], args.get("endTime"))
        records(data, ("time", "delta"))
        return {
            **common,
            "data": data,
            "completeHistory": False,
            "note": "API-bounded Hyperliquid ledger records; not Arbitrum receipt or final settlement verification.",
        }
    if name == "hyperliquid_get_portfolio":
        data = info.portfolio(address)
        if not isinstance(data, list) or any(
            not isinstance(row, list) or len(row) != 2 or not isinstance(row[1], dict) for row in data
        ):
            raise ValueError("Invalid portfolio")
        result = {**common, "data": data, "note": "Historical performance, not spendable balances."}
        if args.get("includeVaultEquities", False):
            equities = info.user_vault_equities(address)
            records(equities, ("vaultAddress", "equity"))
            for row in equities:
                decimal_string(row["equity"])
            result["vaultEquities"] = equities
        return result
    if name == "hyperliquid_get_user_fills":
        selection = args["mode"]
        if selection == "time":
            data = info.user_fills_by_time(
                address=address,
                start_time=args["startTime"],
                end_time=args.get("endTime"),
                aggregate_by_time=args.get("aggregateByTime", False),
            )
            records(data, ("coin", "time", "oid", "px", "sz"))
        elif selection == "twap":
            data = info.user_twap_slice_fills(address)
            records(data, ("fill", "twapId"))
        else:
            data = info.user_fills(address)
            records(data, ("coin", "time", "oid", "px", "sz"))
        return {
            **common,
            "data": data,
            "mode": selection,
            "completeHistory": False,
            "note": "One API-bounded response; not guaranteed complete history.",
        }
    raise ReadInputError("Unknown account query.")


def trading_capacity(handler, market, address):
    if market["marketType"] != "perp":
        return {
            "status": "not_supported",
            "note": "Perp capacity API does not apply to spot; inspect base/quote balances and holds.",
        }
    value = active_asset(handler.info, address, market["symbol"])
    if value is None:
        return {
            "status": "unavailable",
            "userAddress": address,
            "note": "No account-specific capacity returned; this is not zero capacity.",
        }
    return {
        "status": "observed",
        "userAddress": address,
        "observedAt": int(time.time() * 1000),
        "collateral": market["collateral"],
        "leverage": value["leverage"],
        "availableToTrade": value["availableToTrade"],
        "maxTradeSzs": value["maxTradeSzs"],
        "sideOrder": ["buy", "sell"],
        "markPx": value["markPx"],
        "note": "Available-to-trade amounts are collateral units; maxTradeSzs are base quantities. Fresh exchange estimates, not an execution guarantee. Zero does not identify the cause. Check accountMode and collateral location before proposing a transfer or mode change.",
    }
