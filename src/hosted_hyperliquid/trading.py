"""Exact-market execution. Validate the complete batch before one signed submission."""

import time
import uuid
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from hyperliquid.utils.signing import float_to_wire
from hyperliquid.utils.types import Cloid

from .read_validation import active_asset, decimal_string, records


class TradingInputError(ValueError):
    """Static messages safe to return at the tool boundary."""


def require(ok, message):
    if not ok:
        raise TradingInputError(message)


def number(value, *, positive=True):
    try:
        result = decimal_string(value)
    except (ValueError, TypeError):
        raise TradingInputError("Use a finite decimal string.") from None
    require(
        result > 0 if positive else result != 0,
        "Amount must be positive."
        if positive
        else "Margin adjustment must be nonzero; positive adds, negative removes.",
    )
    require(abs(result) < Decimal("1e15"), "Amount is outside supported bounds.")
    return result


def wire_float(value):
    result = float(value)
    require(
        Decimal(float_to_wire(result)) == value,
        "Value cannot be represented exactly by the SDK; reduce precision.",
    )
    return result


def price_valid(price, decimals, kind):
    max_decimals = (6 if kind == "perp" else 8) - decimals
    require(max_decimals >= 0, "Unsupported market precision.")
    require(
        price == price.quantize(Decimal(1).scaleb(-max_decimals)),
        "Price exceeds market decimal precision; read market_details.precision.",
    )
    require(
        price == price.to_integral_value() or len(price.normalize().as_tuple().digits) <= 5,
        "Noninteger prices may have at most five significant figures.",
    )


class Trading:
    def __init__(self, handler):
        self.h = handler
        self.metas = {}
        self.spot = None
        self.address = handler.vault_address or handler.account_address

    def resolve(self, args, *, perp_only=False, allow_delisted=False):
        kind, symbol = args["marketId"].split(":", 1)
        require(not perp_only or kind == "perp", "This operation requires a perpetual market.")
        dex = symbol.split(":", 1)[0] if kind == "perp" and ":" in symbol else ""
        if kind == "perp":
            meta = self.meta(dex)
            matches = [a for a in meta["universe"] if a["name"] == symbol]
            require(len(matches) == 1, "Unknown market. Copy an exact marketId from list_markets.")
            asset = matches[0]
            if not allow_delisted:
                require(not asset.get("isDelisted", False), "Market is delisted; new orders are unavailable.")
        else:
            if self.spot is None:
                self.spot = self.h.info.spot_meta()
            matches = [a for a in self.spot["universe"] if a["name"] == symbol]
            require(
                len(matches) == 1, "Unknown spot market. Copy its exact marketId, not a display-name alias."
            )
            tokens = {t["index"]: t for t in self.spot["tokens"]}
            asset = tokens[matches[0]["tokens"][0]]
        self.h.trading_dexs.add(dex)
        return {"kind": kind, "symbol": symbol, "dex": dex, "asset": asset, "marketId": kind + ":" + symbol}

    def meta(self, dex):
        if dex not in self.metas:
            self.metas[dex] = self.h.info.meta(dex=dex)
        return self.metas[dex]

    def slippage_price(self, market, is_buy, slippage):
        require(0 < slippage <= 0.1, "Slippage must be greater than zero and at most 0.1.")
        if market["kind"] == "perp":
            mid = self.h.info.all_mids(dex=market["dex"]).get(market["symbol"])
        else:
            mid = self.h.info.all_mids().get(market["symbol"])
        require(mid is not None, "No current mid-price is available; no order submitted.")
        mid = number(mid)
        bound = mid * (Decimal(1) + Decimal(str(slippage)) * (1 if is_buy else -1))
        places = (6 if market["kind"] == "perp" else 8) - market["asset"]["szDecimals"]
        # Round toward the mid so rounding never widens the requested bound.
        step = max(Decimal(1).scaleb(-places), Decimal(1).scaleb(bound.adjusted() - 4))
        px = bound.quantize(step, rounding=ROUND_DOWN if is_buy else ROUND_UP)
        require(px > 0, "Slippage price rounded to zero; use an explicit valid limit price.")
        return px

    def order_request(self, args, market):
        size = number(args["size"])
        decimals = market["asset"]["szDecimals"]
        require(
            size == size.quantize(Decimal(1).scaleb(-decimals)),
            "Size exceeds market precision; use market_details.precision.sizeDecimals.",
        )
        reduce = args.get("reduceOnly", False)
        require(not (market["kind"] == "spot" and reduce), "reduceOnly is not supported for spot.")
        kind = args["type"]
        if kind == "market":
            raw_price = self.slippage_price(market, args["isBuy"], args.get("slippage", 0.05))
            order_type = {"limit": {"tif": "Ioc"}}
        else:
            raw_price = number(args["price"])
            order_type = {"limit": {"tif": args.get("timeInForce", "Gtc")}}
            if kind == "trigger":
                require(market["kind"] == "perp", "Trigger orders require a perpetual market.")
                trigger = number(args["triggerPrice"])
                price_valid(trigger, decimals, market["kind"])
                order_type = {
                    "trigger": {
                        "triggerPx": wire_float(trigger),
                        "isMarket": args["triggerIsMarket"],
                        "tpsl": args["triggerKind"],
                    }
                }
        price_valid(raw_price, decimals, market["kind"])
        # Spot quote tokens can differ: let the exchange enforce their notional minimum.
        # Reduce-only dust exits can also be below the normal minimum.
        require(
            market["kind"] == "spot" or reduce or size * raw_price >= 10,
            "Order notional must be at least 10 quote units; collateral is a different amount.",
        )
        return {
            "coin": market["symbol"],
            "is_buy": args["isBuy"],
            "sz": wire_float(size),
            "limit_px": wire_float(raw_price),
            "order_type": order_type,
            "reduce_only": reduce,
            "cloid": Cloid(args.get("cloid", "0x" + uuid.uuid4().hex)),
        }

    def submit(self, method, *args, **kwargs):
        # Constructor/metadata errors occur before entering the ambiguous-send region.
        exchange = self.h.exchange
        try:
            data = getattr(exchange, method)(*args, **kwargs)
        except Exception:  # noqa: BLE001 -- transmission may have succeeded; never retry here
            return {
                "status": "unknown",
                "error": "Submission outcome unknown; it may have executed. Check relevant orders/fills, account mode, or ledger/balances before retrying.",
            }
        if not isinstance(data, dict) or data.get("status") not in {"ok", "err"}:
            return {
                "status": "unknown",
                "error": "Unexpected submission response. Reconcile before retrying.",
            }
        if data.get("status") == "ok":
            response = data.get("response")
            valid = isinstance(response, dict)
            if method.startswith("bulk_"):
                statuses = (
                    response.get("data", {}).get("statuses")
                    if valid and isinstance(response.get("data"), dict)
                    else None
                )
                valid = (
                    isinstance(statuses, list)
                    and len(statuses) == len(args[0])
                    and all(
                        status == "success"
                        or isinstance(status, dict)
                        and (
                            isinstance(status.get("error"), str)
                            or isinstance(status.get("resting"), dict)
                            and type(status["resting"].get("oid")) is int
                            or isinstance(status.get("filled"), dict)
                            and type(status["filled"].get("oid")) is int
                        )
                        for status in statuses
                    )
                )
            else:
                valid = valid and response.get("type") == "default"
            if not valid:
                return {
                    "status": "unknown",
                    "error": "Incomplete exchange acknowledgement. Reconcile before retrying.",
                    "exchangeResponse": data,
                }
        return data

    def execute(self, name, args):
        if name == "hyperliquid_place_bracket_order":
            return self.bracket(args)
        if name == "hyperliquid_cancel_all_orders":
            dex = args["dex"]
            orders = self.h.info.open_orders(self.address, dex=dex)
            records(orders, ("coin", "oid"))
            require(
                all((o["coin"].split(":", 1)[0] if ":" in o["coin"] else "") == dex for o in orders),
                "Open orders do not match the selected deployment.",
            )
            if not orders:
                return {"data": {"status": "ok", "response": {"data": {"statuses": []}}}, "requestedCount": 0}
            self.h.trading_dexs.add(dex)
            requests = [{"coin": o["coin"], "oid": o["oid"]} for o in orders]
            return {"data": self.submit("bulk_cancel", requests), "requestedCount": len(requests)}
        if name == "hyperliquid_schedule_cancel":
            when = int(args["time"]) if args["time"] is not None else None
            require(
                when is None or when >= int(time.time() * 1000) + 5000,
                "Cancellation time must be at least five seconds in the future; null clears the schedule.",
            )
            return {
                "data": self.submit("schedule_cancel", when),
                "scheduledTime": when,
                "note": "Exchange-wide scheduled cancellation for this account, not a position close. Submission is not confirmation that the schedule fired.",
            }
        if name in {
            "hyperliquid_update_leverage",
            "hyperliquid_update_isolated_margin",
            "hyperliquid_close_position",
        }:
            market = self.resolve(args, perp_only=True)
            symbol = market["symbol"]
            if name == "hyperliquid_update_leverage":
                require(
                    args["leverage"] <= market["asset"]["maxLeverage"],
                    "Requested leverage exceeds the market ceiling; inspect market_details.maxLeverage and marginTable.",
                )
                require(
                    not (
                        args["isCross"]
                        and (
                            market["asset"].get("onlyIsolated")
                            or market["asset"].get("marginMode") in {"isolated", "strictIsolated", "noCross"}
                        )
                    ),
                    "This market requires isolated margin.",
                )
                data = self.submit("update_leverage", args["leverage"], symbol, is_cross=args["isCross"])
                verification = {"status": "not_verified"}
                if data.get("status") == "ok":
                    try:
                        active = active_asset(self.h.info, self.address, symbol)
                        observed = active["leverage"] if active else None
                        expected = {
                            "type": "cross" if args["isCross"] else "isolated",
                            "value": args["leverage"],
                        }
                        verification = {
                            "status": "verified"
                            if observed and all(observed.get(k) == v for k, v in expected.items())
                            else "not_verified",
                            "observedLeverage": observed,
                        }
                    except Exception:  # noqa: BLE001 -- read failure must not hide an accepted write
                        verification = {"status": "read_failed_do_not_resubmit"}
                return {"data": data, "marketId": market["marketId"], "verification": verification}
            state = self.h.info.user_state(self.address, dex=market["dex"])
            position = next(
                (p["position"] for p in state["assetPositions"] if p["position"]["coin"] == symbol), None
            )
            require(
                position is not None and decimal_string(position["szi"]) != 0,
                "No open position exists for this market.",
            )
            if name == "hyperliquid_update_isolated_margin":
                require(
                    position["leverage"]["type"] == "isolated",
                    "Margin adjustment requires an isolated position.",
                )
                amount = number(args["amount"], positive=False)
                require(
                    amount == amount.quantize(Decimal(".000001")),
                    "Margin adjustment supports at most six decimals.",
                )
                return {
                    "data": self.submit("update_isolated_margin", wire_float(amount), symbol),
                    "marketId": market["marketId"],
                }
            held = decimal_string(position["szi"])
            size = number(args["size"]) if "size" in args else abs(held)
            require(size <= abs(held), "Close size exceeds the current position.")
            order = self.order_request(
                {
                    "type": "market",
                    "size": str(size),
                    "isBuy": held < 0,
                    "reduceOnly": True,
                    "slippage": args.get("slippage", 0.05),
                },
                market,
            )
            return {
                "data": self.submit("bulk_orders", [order]),
                "marketId": market["marketId"],
                "clientOrderIds": [order["cloid"].to_raw()],
                "note": "Inspect exchange status and fills; a reduce-only IOC can be rejected or partially filled. Re-read the remaining position. Closing does not reset leverage.",
            }
        items = args["orders"]
        require(1 <= len(items) <= 50, "Batch must contain 1 to 50 items.")
        markets = [self.resolve(item, allow_delisted=name == "hyperliquid_cancel_orders") for item in items]
        if name == "hyperliquid_place_orders":
            orders = [self.order_request(item, market) for item, market in zip(items, markets, strict=True)]
            ids = [o["cloid"].to_raw() for o in orders]
            require(len(set(ids)) == len(ids), "Client order IDs must be unique within a batch.")
            return {
                "data": self.submit("bulk_orders", orders),
                "clientOrderIds": ids,
                "note": "Inspect every status; batches are not atomic. Reconcile by client order ID after an unknown outcome.",
            }
        if name == "hyperliquid_modify_orders":
            modifies = []
            for item, market in zip(items, markets, strict=True):
                require(decimal_string(item["price"]) > 0, "Modification requires a positive limit price.")
                request = self.order_request(item, market)
                request.pop("cloid")  # keep the existing order identity
                modifies.append(
                    {"oid": int(item["oid"]) if "oid" in item else Cloid(item["cloid"]), "order": request}
                )
            require(
                len({str(m["oid"]) for m in modifies}) == len(modifies),
                "An order may only appear once per batch.",
            )
            return {"data": self.submit("bulk_modify_orders_new", modifies), "requestedCount": len(modifies)}
        if name == "hyperliquid_cancel_orders":
            by_cloid = "cloid" in items[0]
            require(
                all(("cloid" in item) == by_cloid for item in items),
                "A cancellation batch must use only order IDs or only client order IDs.",
            )
            key = "cloid" if by_cloid else "oid"
            requests = [
                {"coin": m["symbol"], key: Cloid(a[key]) if by_cloid else int(a[key])}
                for a, m in zip(items, markets, strict=True)
            ]
            require(
                len({(a["coin"], str(a[key])) for a in requests}) == len(requests),
                "Duplicate cancellation in batch.",
            )
            return {
                "data": self.submit("bulk_cancel_by_cloid" if by_cloid else "bulk_cancel", requests),
                "requestedCount": len(requests),
            }
        raise TradingInputError("Unsupported trading operation.")

    def bracket(self, args):
        entry_args = args["entry"]
        require(entry_args["type"] in {"market", "limit"}, "Bracket entry must be a market or limit order.")
        require(not entry_args.get("reduceOnly", False), "Bracket entry cannot be reduce-only.")
        market = self.resolve(entry_args, perp_only=True)
        entry = self.order_request(entry_args, market)
        orders = [entry]
        for field, kind in [("takeProfitPrice", "tp"), ("stopLossPrice", "sl")]:
            orders.append(
                self.order_request(
                    {
                        "type": "trigger",
                        "size": entry_args["size"],
                        "price": args[field],
                        "isBuy": not entry_args["isBuy"],
                        "reduceOnly": True,
                        "triggerPrice": args[field],
                        "triggerIsMarket": False,
                        "triggerKind": kind,
                    },
                    market,
                )
            )
        return {
            "data": self.submit("bulk_orders", orders, grouping="normalTpsl"),
            "clientOrderIds": [o["cloid"].to_raw() for o in orders],
            "note": "Linked bracket orders; inspect each status. Batch acceptance is not atomic.",
        }
