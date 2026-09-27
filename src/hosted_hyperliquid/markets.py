"""Read-only discovery across perp deployments and spot, without SDK symbol aliases."""

import time

import requests

from .read_validation import ReadInputError, decimal_string, metadata, records


def require(condition):
    if not condition:
        raise ValueError("Invalid market metadata")


def token_map(meta):
    tokens = records(meta["tokens"], ("index", "name", "szDecimals"))
    result = {t["index"]: t for t in tokens}
    require(len(result) == len(tokens))
    for t in tokens:
        require(type(t["index"]) is int and isinstance(t["name"], str))
        require(type(t["szDecimals"]) is int and 0 <= t["szDecimals"] <= 8)
    return result


def token_summary(token):
    return {key: token.get(key) for key in ("index", "name", "fullName", "tokenId")}


def deployments(data):
    require(isinstance(data, list) and data and data[0] is None)
    result = [{"name": "", "fullName": "Hyperliquid"}] + records(data[1:], ("name", "fullName"))
    require(len({d["name"] for d in result}) == len(result))
    return result


def perp_rows(meta, deployment):
    metadata(meta)
    rows = []
    for asset in meta["universe"]:
        dex = deployment["name"]
        require(asset["name"].split(":", 1)[0] == dex if dex else ":" not in asset["name"])
        require(type(asset.get("isDelisted", False)) is bool)
        rows.append(
            {
                "marketId": "perp:" + asset["name"],
                "symbol": asset["name"],
                "displayName": asset["name"],
                "marketType": "perp",
                "dex": dex,
                "deploymentName": deployment["fullName"],
                "status": "delisted" if asset.get("isDelisted", False) else "listed",
                "maxLeverage": asset["maxLeverage"],
                "supportedByOrderTools": not asset.get("isDelisted", False),
            }
        )
    return rows


def spot_rows(meta):
    tokens = token_map(meta)
    rows = []
    for asset in records(meta["universe"], ("name", "tokens", "index")):
        require(isinstance(asset["name"], str) and len(asset["tokens"]) == 2)
        base, quote = (tokens[index] for index in asset["tokens"])
        rows.append(
            {
                "marketId": "spot:" + asset["name"],
                "symbol": asset["name"],
                "displayName": base["name"] + "/" + quote["name"],
                "marketType": "spot",
                "dex": None,
                "deploymentName": "Hyperliquid spot",
                "status": "listed",
                "maxLeverage": None,
                "supportedByOrderTools": True,
            }
        )
    require(len({r["marketId"] for r in rows}) == len(rows))
    return rows


def annotation_map(data, *, spot=False):
    require(isinstance(data, list))
    result = {}
    for item in data:
        require(isinstance(item, list) and len(item) == 2)
        key, value = item
        require(type(key) is (int if spot else str) and isinstance(value, dict))
        require(key not in result)
        for field in ("displayName", "category"):
            require(field not in value or isinstance(value[field], str))
        keywords = value.get("keywords", [])
        require(isinstance(keywords, list) and all(isinstance(word, str) for word in keywords))
        result[key] = value
    return result


def context(value):
    require(isinstance(value, dict) and "markPx" in value)
    result = {}
    for key in (
        "markPx",
        "midPx",
        "oraclePx",
        "prevDayPx",
        "funding",
        "openInterest",
        "dayNtlVlm",
        "dayBaseVlm",
    ):
        number = value.get(key)
        if number is not None:
            decimal_string(number)
        result[key] = number
    return result


class MarketReader:
    def __init__(self, session, base_url, timeout):
        self.session, self.url, self.timeout = session, base_url.rstrip("/") + "/info", timeout

    def post(self, kind, **params):
        response = self.session.post(
            self.url, json={"type": kind, **params}, timeout=self.timeout, allow_redirects=False
        )
        require(response.status_code == 200)
        return response.json()

    def enrich(self, rows, spot_meta=None):
        # One bulk request per market kind; annotations never control execution identity.
        perps = any(row["marketType"] == "perp" for row in rows)
        spots = any(row["marketType"] == "spot" for row in rows)
        try:
            perp_annotations = annotation_map(self.post("perpConciseAnnotations")) if perps else {}
            spot_annotations = (
                annotation_map(self.post("tokenConciseAnnotations"), spot=True) if spots else {}
            )
        except Exception:  # noqa: BLE001 -- partial search must not masquerade as no matches
            raise ReadInputError(
                "Market annotations unavailable or invalid. Search coverage is unknown; retry the read before concluding a company or category is absent."
            ) from None
        tokens = token_map(spot_meta) if spots else {}
        pairs = {asset["name"]: asset["tokens"] for asset in spot_meta["universe"]} if spots else {}
        for row in rows:
            if row["marketType"] == "perp":
                annotation = perp_annotations.get(row["symbol"], {})
                keywords = annotation.get("keywords", [])
                row["displayName"] = annotation.get("displayName") or row["displayName"]
            else:
                base, quote = (tokens[index] for index in pairs[row["symbol"]])
                annotation = spot_annotations.get(base["index"], {})
                quote_annotation = spot_annotations.get(quote["index"], {})
                row["displayName"] = (
                    (annotation.get("displayName") or base["name"])
                    + "/"
                    + (quote_annotation.get("displayName") or quote["name"])
                )
                keywords = [
                    *annotation.get("keywords", []),
                    *quote_annotation.get("keywords", []),
                    base["name"],
                    quote["name"],
                    base.get("fullName"),
                    quote.get("fullName"),
                ]
            row["category"] = annotation.get("category") or None
            row["keywords"] = list(dict.fromkeys(word for word in keywords if word))

    def listing(self, args):
        kind, dex = args.get("marketType", "all"), args.get("dex")
        if kind == "spot" and dex is not None:
            raise ReadInputError("dex applies only to perpetual markets; omit it for spot.")
        rows, spot_meta = [], None
        if kind != "spot":
            dexs = deployments(self.post("perpDexs"))
            if dex is not None:
                selected = next((d for d in dexs if d["name"] == dex), None)
                if selected is None:
                    raise ReadInputError(
                        "Unknown deployment name. Use list_markets without dex to discover deployments."
                    )
                rows.extend(perp_rows(self.post("meta", dex=dex), selected))
            else:
                # allPerpMetas is a live API aggregation. Fail closed if deployment
                # count/order changes rather than mislabelling a market's collateral.
                metas = self.post("allPerpMetas")
                require(isinstance(metas, list) and len(metas) == len(dexs))
                for meta, deployment in zip(metas, dexs, strict=True):
                    rows.extend(perp_rows(meta, deployment))
        if kind != "perp" and dex is None:
            spot_meta = self.post("spotMeta")
            rows.extend(spot_rows(spot_meta))
        self.enrich(rows, spot_meta)
        rows = [row for row in rows if args.get("includeDelisted", False) or row["status"] != "delisted"]
        categories = sorted({row["category"] for row in rows if row["category"]})
        category = args.get("category", "").strip().casefold()
        search = args.get("search", "").strip().casefold()
        rows = [
            r
            for r in rows
            if (not category or (r["category"] or "").casefold() == category)
            and search
            in " ".join(
                [
                    *(str(r[k] or "") for k in ("symbol", "displayName", "deploymentName", "dex")),
                    *r["keywords"],
                ]
            ).casefold()
        ]
        rows.sort(key=lambda r: r["marketId"])
        offset, limit = args.get("offset", 0), args.get("limit", 20)
        return {
            "markets": rows[offset : offset + limit],
            "total": len(rows),
            "categories": categories,
            "offset": offset,
            "nextOffset": offset + limit if offset + limit < len(rows) else None,
            "observedAt": int(time.time() * 1000),
            "note": "Search matches symbols, display names, deployment names and upstream keywords. Categories are upstream labels available in the selected scope before search/category filters; null means unclassified. Annotations may be incomplete; no match is not proof of absence. Delisted markets are excluded unless requested. Follow nextOffset; use exact marketId for orders. Listing is not trading eligibility. Snapshots may change between pages.",
        }

    def details(self, args):
        kind, symbol = args["marketId"].split(":", 1)
        if kind == "perp":
            dex = symbol.split(":", 1)[0] if ":" in symbol else ""
            deployment = next((d for d in deployments(self.post("perpDexs")) if d["name"] == dex), None)
            if deployment is None:
                raise ReadInputError("Unknown deployment. Copy an exact marketId from list_markets.")
            pair = self.post("metaAndAssetCtxs", dex=dex)
            require(isinstance(pair, list) and len(pair) == 2)
            meta, contexts = pair
            rows = perp_rows(meta, deployment)
            require(isinstance(contexts, list) and len(contexts) == len(rows))
            index = next((i for i, row in enumerate(rows) if row["symbol"] == symbol), None)
            if index is None:
                raise ReadInputError("Market not found. Copy an exact marketId from list_markets.")
            result, asset = rows[index], meta["universe"][index]
            prices = context(contexts[index])
            tokens = token_map(self.post("spotMeta"))
            collateral = token_summary(tokens[meta["collateralToken"]])
            tables = dict(meta.get("marginTables", []))
            margin_table = tables.get(asset.get("marginTableId"))
            decimals = asset["szDecimals"]
            extra = {
                "collateral": collateral,
                "marginTable": margin_table,
                "onlyIsolated": asset.get("onlyIsolated", False),
                "marginMode": asset.get("marginMode"),
                "baseToken": None,
                "quoteToken": None,
            }
        else:
            pair = self.post("spotMetaAndAssetCtxs")
            require(isinstance(pair, list) and len(pair) == 2)
            meta, contexts = pair
            rows = spot_rows(meta)
            index = next((i for i, row in enumerate(rows) if row["symbol"] == symbol), None)
            if index is None:
                raise ReadInputError("Market not found. Copy an exact marketId from list_markets.")
            result, asset = rows[index], meta["universe"][index]
            # Context arrays may include markets absent from universe: never zip.
            matches = [c for c in records(contexts, ("coin",)) if c["coin"] == symbol]
            require(len(matches) == 1)
            prices = context(matches[0])
            tokens = token_map(meta)
            base, quote = (tokens[i] for i in asset["tokens"])
            decimals = base["szDecimals"]
            extra = {
                "baseToken": token_summary(base),
                "quoteToken": token_summary(quote),
                "collateral": None,
                "marginTable": None,
                "onlyIsolated": None,
                "marginMode": None,
            }
        self.enrich([result], meta if kind == "spot" else None)
        require(type(decimals) is int and 0 <= decimals <= (6 if kind == "perp" else 8))
        return {
            **result,
            **extra,
            "prices": prices,
            "precision": {
                "sizeDecimals": decimals,
                "maxPriceDecimals": (6 if kind == "perp" else 8) - decimals,
                "maxPriceSignificantFigures": 5,
                "integerPricesAlwaysAllowed": True,
            },
            "tradingEnabled": False if result["status"] == "delisted" else None,
            "observedAt": int(time.time() * 1000),
            "note": "Trading availability/session halts are not established by metadata; null means unknown. Prices are decimal strings; mid/oracle may be unavailable. maxLeverage is a ceiling; margin tiers can lower it. Use exact marketId for orders. Spot orders do not support perp leverage, reduce-only, or brackets.",
        }


def read_markets(base_url, arguments, timeout, *, details=False):
    with requests.Session() as session:
        reader = MarketReader(session, base_url, timeout)
        return reader.details(arguments) if details else reader.listing(arguments)
