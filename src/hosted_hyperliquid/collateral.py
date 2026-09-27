"""Explicit collateral transfers and account-mode configuration."""

import re
import time
from decimal import Context, Decimal

from .account_reads import mode, spot_state
from .markets import deployments, token_map
from .read_validation import account_state
from .trading import Trading, number, require


def collateral_snapshot(handler, locations):
    info, address = handler.info, handler.wallet.address
    result = {
        "userAddress": address,
        "accountMode": mode(info, address),
        "observedAt": int(time.time() * 1000),
        "balances": {},
    }
    for location in sorted(locations):
        if location == "spot":
            result["balances"][location] = spot_state(info, address)
        else:
            state = info.user_state(address, dex=location)
            account_state(state)
            result["balances"][location] = {
                "reportedMarginSummary": state["marginSummary"],
                "reportedWithdrawable": state["withdrawable"],
            }
    return result


def transfer_collateral(handler, args):
    require(
        not handler.vault_address and handler.account_address.lower() == handler.wallet.address.lower(),
        "Collateral transfers require the owner's key without account or vault overrides.",
    )
    source, destination = args["source"], args["destination"]
    require(source != destination, "Source and destination must differ.")
    amount = number(args["amount"])
    locations = {source, destination}
    known = {d["name"] for d in deployments(handler.info.perp_dexs())} | {"spot"}
    require(locations <= known, "Unknown deployment; use exact deployment names from list_markets.")
    token = token_map(handler.info.spot_meta()).get(args["tokenIndex"])
    require(token is not None, "Unknown token index; use market_details collateral.index.")
    token_id, precision = token.get("tokenId"), token.get("weiDecimals")
    require(
        isinstance(token_id, str)
        and re.fullmatch(r"0x[0-9a-fA-F]{1,32}", token_id)
        and type(precision) is int
        and 0 <= precision <= 18,
        "Invalid token transfer metadata.",
    )
    require(
        amount == amount.quantize(Decimal(1).scaleb(-precision), context=Context(prec=40)),
        "Amount exceeds the token's transfer precision.",
    )
    for location in sorted(locations - {"spot"}):
        meta = handler.info.meta(dex=location)
        require(
            type(meta.get("collateralToken")) is int and meta["collateralToken"] == token["index"],
            "Token does not match the deployment's collateral; no transfer submitted.",
        )
    before = collateral_snapshot(handler, locations)
    result = {
        "source": source,
        "destination": destination,
        "tokenIndex": token["index"],
        "token": token["name"] + ":" + token_id,
        "amount": format(amount, "f"),
        "before": before,
    }
    if before["accountMode"] in {"unifiedAccount", "portfolioMargin"}:
        return {
            **result,
            "status": "not_applicable",
            "submitted": False,
            "accountMode": before["accountMode"],
            "message": "Compatible collateral is shared in this mode. No transfer submitted; account mode was not changed.",
        }
    # The SDK stringifies amount; preserve its decimal representation without a float conversion.
    data = Trading(handler).submit(
        "send_asset", handler.wallet.address, source, destination, result["token"], result["amount"]
    )
    status = (
        "submitted"
        if data.get("status") == "ok"
        else "rejected"
        if data.get("status") == "err"
        else "unknown"
    )
    result.update(
        data=data,
        status=status,
        verification={"status": "not_checked"},
        note="Same-wallet collateral transfer. Submission and balance observations are separate evidence. Never retry an unknown outcome without checking ledger and balances. Refresh target-market capacity before trading; account mode is unchanged.",
    )
    if status != "rejected":
        try:
            result["after"] = collateral_snapshot(handler, locations)
            result["verification"] = {
                "status": "balances_observed",
                "transferConfirmed": False,
                "note": "Fresh balances are not transaction-specific confirmation; concurrent activity may affect them.",
            }
        except Exception:  # noqa: BLE001 -- preserve submission outcome if the follow-up read fails
            result["verification"] = {"status": "read_failed_do_not_resubmit", "transferConfirmed": False}
    return result


def set_account_mode(handler, args):
    require(
        not handler.vault_address and handler.account_address.lower() == handler.wallet.address.lower(),
        "Account-mode changes require the owner's key without account or vault overrides.",
    )
    target = {"unified": "unifiedAccount", "standard": "disabled"}[args["mode"]]
    address = handler.wallet.address
    before = mode(handler.info, address)
    require(
        before != "portfolioMargin", "Account already uses portfolio margin; this tool will not downgrade it."
    )
    result = {
        "userAddress": address,
        "requestedMode": args["mode"],
        "requestedApiMode": target,
        "beforeMode": before,
        "note": "Account-wide mode change: Unified shares compatible collateral; Standard separates spot and perp deployment balances. If rejected, report the exchange reason; do not infer that an open position caused it. It does not place a trade or guarantee market capacity. Re-read get_account and market_details with includeAccount=true before trading. Balances may change location; switching back need not restore earlier locations.",
    }
    if before == target:
        return {
            **result,
            "status": "already_configured",
            "submitted": False,
            "verification": {
                "status": "verified",
                "observedMode": before,
                "observedAt": int(time.time() * 1000),
            },
        }
    data = Trading(handler).submit("user_set_abstraction", address, target)
    result.update(
        data=data,
        submitted=True,
        status="submitted"
        if data.get("status") == "ok"
        else "rejected"
        if data.get("status") == "err"
        else "unknown",
        verification={"status": "not_checked"},
    )
    if result["status"] != "rejected":
        try:
            observed = mode(handler.info, address)
            result["verification"] = {
                "status": "verified" if observed == target else "not_verified",
                "observedMode": observed,
                "observedAt": int(time.time() * 1000),
            }
        except Exception:  # noqa: BLE001 -- preserve write outcome, never retry an ambiguous submission
            result["verification"] = {"status": "read_failed_do_not_resubmit"}
    return result
