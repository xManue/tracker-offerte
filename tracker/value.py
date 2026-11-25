from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import os

from .catalog import catalog, match
from .channels import fee_for
from .db import now
from .store import events, latest, record, utc

MIN_COMPARABLES = 3
METHOD = "median-v1"
CENT = Decimal("0.01")


def dec(value):
    return Decimal(str(value)) if value is not None else None


def rounded(value):
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP)) if value is not None else None


def eur(row, field):
    value = dec(row.get(field))
    if value is None:
        return None
    if row.get("currency", "EUR") == "EUR":
        return value
    exchange = dec(row.get("fx_rate"))
    return value * exchange if exchange and row.get("fx_date") and row.get("fx_source") else None


def delivered(row):
    price, shipping = eur(row, "price"), eur(row, "shipping")
    return price + shipping if price is not None and shipping is not None else None


def quantile(values, percentile):
    values = sorted(values)
    position = Decimal(len(values) - 1) * Decimal(percentile)
    lower = int(position)
    fraction = position - lower
    return values[lower] * (1 - fraction) + values[min(lower + 1, len(values) - 1)] * fraction


def identification(session, mode, offer, products, cutoff=None):
    result = match(offer.data, products)
    overrides = events(session, mode, "matching", source=offer.source, external_id=offer.external_id, cutoff=cutoff)
    if overrides:
        chosen = overrides[-1].data["product_id"]
        if chosen in products or chosen is None:
            result["product_id"] = chosen
            result["manual"] = True
    return result


def sales_features(session, mode, product_id, condition, market, channel, cutoff,
                   *, exclude=None, exclude_order=None, allowed_sources=None, sale_format=""):
    products = catalog(session, mode, cutoff)
    product = products.get(product_id, {})
    attrs = product.get("attrs", {})
    match_group = (attrs.get("chip"), attrs.get("memory_gb"))
    exact, related = [], []
    floor = cutoff - timedelta(days=180)
    for sale in latest(session, mode, "sale", cutoff=cutoff):
        if utc(sale.effective_at) < floor or (sale.source, sale.external_id) == exclude:
            continue
        if allowed_sources is not None and sale.source not in allowed_sources:
            continue
        row = sale.data
        if exclude_order and exclude_order == (sale.source, row.get("order_id")):
            continue
        if not row["verified"] or row["status"] != "sold":
            continue
        if (row.get("destination") or sale.source).casefold() != channel.casefold():
            continue
        if condition == "unknown" or row["market"] != market or row["condition"] != condition:
            continue
        if sale_format and row.get("sale_format") != sale_format:
            continue
        price = eur(row, "paid_price")
        if price is None:
            continue
        variant = match(row, products)["product_id"]
        if variant is None:
            continue
        facts = (sale, price / row["quantity"])
        if variant == product_id:
            exact.append(facts)
        elif match_group[0] and match_group == tuple(products[variant]["attrs"].get(key) for key in ("chip", "memory_gb")):
            related.append(facts)
    expanded = len(exact) < MIN_COMPARABLES and bool(related)
    evidence = exact + related if expanded else exact
    values = [price for _, price in evidence]
    return {
        "count": len(values), "exact_count": len(exact), "expanded": expanded,
        "method": METHOD, "window_days": 180, "sale_format": sale_format,
        "span_days": (max(pair[0].effective_at for pair in evidence) -
                      min(pair[0].effective_at for pair in evidence)).days if evidence else 0,
        "median": rounded(quantile(values, "0.5")) if values else None,
        "low": rounded(quantile(values, "0.25")) if values else None,
        "high": rounded(quantile(values, "0.75")) if values else None,
        "evidence": [{"id": item.id, "date": utc(item.effective_at).isoformat(), "source": item.source,
                      "unit_eur": rounded(price), "sale_format": item.data["sale_format"]} for item, price in evidence],
    }


def selling_costs(mode, channel):
    rate = fee_for(channel, mode)
    shipping = os.getenv(f"TRACKER_OUTBOUND_{channel.upper().replace('-', '_')}")
    if shipping is None and mode == "demo" and channel in {"ebay", "subito"}:
        shipping = "9.90" if channel == "ebay" else "0"
    extra = dec(os.getenv("TRACKER_EXTRA_EUR", "0"))
    shipping = dec(shipping)
    if extra < 0 or (shipping is not None and shipping < 0):
        raise ValueError("I costi di vendita devono essere positivi o zero")
    return rate, shipping, extra


def margin(gross, purchase, rate, shipping, extra):
    if any(part is None for part in (gross, purchase, rate, shipping, extra)):
        return None
    commission = (gross * rate).quantize(CENT, rounding=ROUND_HALF_UP)
    net = gross - commission - shipping - extra
    profit = net - purchase
    return {"purchase": rounded(purchase), "gross": rounded(gross), "fee": rounded(commission),
            "fee_rate": str(rate), "shipping": rounded(shipping), "extra": rounded(extra),
            "net": rounded(net), "profit": rounded(profit),
            "percent": rounded(100 * profit / purchase) if purchase else None}


def assess(session, mode, offer, cutoff=None):
    cutoff = utc(cutoff or now())
    products = catalog(session, mode, cutoff)
    matching = identification(session, mode, offer, products, cutoff)
    product_id = matching["product_id"]
    row = offer.data
    purchase = delivered(row)
    channels = {"ebay"}
    channels.update((item.data.get("destination") or item.source).casefold()
                    for item in latest(session, mode, "sale", cutoff=cutoff) if item.data.get("verified"))
    estimates = []
    if product_id and row.get("condition") != "broken":
        for channel in sorted(channels):
            comp = sales_features(session, mode, product_id, row["condition"], row["market"], channel, cutoff)
            if not comp["count"]:
                continue
            amounts = margin(dec(comp["median"]), purchase, *selling_costs(mode, channel))
            estimates.append({"channel": channel, "comparables": comp, "margin": amounts,
                              "confidence": "buona" if comp["count"] >= MIN_COMPARABLES and not comp["expanded"] else "bassa"})
    estimates.sort(key=lambda item: (item["margin"] is not None,
                                     dec(item["margin"]["profit"]) if item["margin"] else Decimal("-9999999999")),
                   reverse=True)
    return {"offer_id": offer.id, "matching": matching, "purchase": rounded(purchase), "estimates": estimates,
            "method": METHOD, "evaluated_at": cutoff.isoformat(), "observed_at": utc(offer.known_at).isoformat(),
            "status": row["status"]}


def save_assessment(session, mode, offer):
    assessment = assess(session, mode, offer)
    identity = {key: value for key, value in assessment.items() if key != "evaluated_at"}
    from .store import canonical
    return record(session, mode, "valuation", offer.source, offer.external_id, assessment,
                  identity=canonical(identity)), assessment


def liquidity(session, mode, product_id, channel, ask, cutoff=None, condition="used", market="IT"):
    cutoff = utc(cutoff or now())
    left, right = ask * Decimal("0.9"), ask * Decimal("1.1")
    products = catalog(session, mode, cutoff)
    checks = {}
    for item in events(session, mode, "offer", cutoff=cutoff, source=channel):
        checks.setdefault(item.external_id, []).append(item)
    outcomes = {}
    for sale in latest(session, mode, "sale", cutoff=cutoff):
        row = sale.data
        if sale.source == channel and row.get("listing_id") and row["verified"] and row["status"] == "sold":
            outcomes.setdefault(row["listing_id"], []).append(utc(sale.effective_at))
    hits = trials = 0
    for identifier, timeline in checks.items():
        initial = timeline[0].data
        published = initial.get("published_at")
        price = eur(initial, "price")
        if initial["market"] != market or initial["condition"] != condition or price is None or not left <= price <= right:
            continue
        if match(initial, products)["product_id"] != product_id or not published:
            continue
        from datetime import datetime
        start = datetime.fromisoformat(published)
        end = start + timedelta(days=30)
        if end > cutoff or utc(timeline[0].known_at) - start > timedelta(days=1):
            continue
        known_sale = [sold for sold in outcomes.get(identifier, []) if start <= sold <= end]
        if known_sale:
            terminal = min(known_sale)
            if all(eur(item.data, "price") == price for item in timeline if utc(item.known_at) <= terminal):
                hits += 1
                trials += 1
        else:
            observed = [item for item in timeline if utc(item.known_at) <= end + timedelta(days=1)]
            if observed and utc(observed[-1].known_at) >= end and all(
                item.data["status"] == "available" and eur(item.data, "price") == price for item in observed
            ) and all(utc(b.known_at) - utc(a.known_at) <= timedelta(days=3) for a, b in zip(observed, observed[1:])):
                trials += 1
    return {"trials": trials, "hits": hits, "probability": rounded(Decimal(hits) / trials) if trials >= 10 else None}
