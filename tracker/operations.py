from collections import Counter
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from .catalog import catalog, clean, match
from .channels import CHANNELS
from .db import Reservation, now
from .ingest import ROME
from .store import events, latest, record, utc
from .value import assess, dec, delivered, liquidity, margin, rounded, sales_features, selling_costs


def reference(offer):
    return f"{offer.source}:{offer.external_id}"


def duplicate_groups(session, mode, cutoff=None):
    return {item.external_id: item.data["group"]
            for item in latest(session, mode, "duplicate", cutoff=cutoff) if item.data.get("group")}


def duplicate_candidates(target, offers):
    row = target.data
    if not row.get("photo_hash"):
        return []
    matches = []
    for offer in offers:
        other = offer.data
        if target.id == offer.id or not other.get("photo_hash"):
            continue
        try:
            distance = (int(row["photo_hash"], 16) ^ int(other["photo_hash"], 16)).bit_count()
        except ValueError:
            continue
        same_title = clean(row["title"]) == clean(other["title"])
        same_area = bool(row.get("area") and row["area"] == other.get("area"))
        same_price = row.get("price") is not None and row.get("price") == other.get("price") and row["currency"] == other["currency"]
        if distance <= 6 and (same_title or (same_area and same_price)):
            matches.append({"offer": offer, "distance": distance, "same_title": same_title,
                            "same_area": same_area, "same_price": same_price})
    return matches


def offer_cards(session, mode, cutoff=None):
    groups = duplicate_groups(session, mode, cutoff)
    products = catalog(session, mode, cutoff)
    cards = []
    for offer in latest(session, mode, "offer", cutoff=cutoff):
        evaluation = assess(session, mode, offer, cutoff)
        matching = evaluation["matching"]
        product = products.get(matching["product_id"])
        best = next(iter(evaluation["estimates"]), None)
        cards.append({"offer": offer, "assessment": evaluation, "product": product,
                      "best": best, "group": groups.get(reference(offer), reference(offer))})
    return cards


def plan_budget(session, mode, cards, capital, minimum, concentration, horizon, *, prudent=False):
    chosen, excluded, seen = [], [], set()
    used, net = Decimal(0), Decimal(0)
    by_category = Counter()
    inventory_groups = {row.data.get("group") for row in latest(session, mode, "inventory")
                        if row.data["state"] in {"purchased", "verified", "listed", "repriced", "returned"}}
    reserved = {item for plan in latest(session, mode, "build") if plan.data.get("reserved")
                for item in plan.data["refs"]}
    reserved_groups = set(session.scalars(select(Reservation.unit).where(Reservation.mode == mode)))
    eligible = []
    for card in cards:
        row = card["offer"].data
        best = card["best"]
        reason = None
        if card["group"] in inventory_groups | reserved_groups or reference(card["offer"]) in reserved:
            reason = "Articolo già impegnato"
        elif row["status"] != "available" or row.get("quantity") == 0:
            reason = "Disponibilità da verificare"
        elif not best or not best["margin"] or not card["product"]:
            reason = "Stima o costi insufficienti"
        elif prudent and best["confidence"] != "buona":
            reason = "Evidenza insufficiente per lo scenario prudente"
        if reason:
            excluded.append({"title": row["title"], "reason": reason})
            continue
        amount = best["margin"]
        gross = dec(best["comparables"]["low"]) if prudent else dec(amount["gross"])
        purchase = dec(amount["purchase"])
        costs = margin(gross, purchase, dec(amount["fee_rate"]), dec(amount["shipping"]), dec(amount["extra"]))
        profit = dec(costs["profit"])
        probability = liquidity(session, mode, card["product"]["id"], best["channel"], gross,
                                condition=row["condition"], market=row["market"])["probability"] if horizon == 30 else None
        eligible.append((card, purchase, profit, dec(probability)))
    eligible.sort(key=lambda item: (item[2] * (item[3] if item[3] is not None else Decimal(1)), item[2]), reverse=True)
    for card, purchase, profit, probability in eligible:
        title = card["offer"].data["title"]
        category = card["product"]["category"]
        if card["group"] in seen:
            reason = "Stessa unità fisica"
        elif profit < minimum:
            reason = "Margine sotto soglia"
        elif used + purchase > capital:
            reason = "Capitale insufficiente"
        elif by_category[category] + purchase > capital * concentration:
            reason = "Limite per categoria"
        else:
            chosen.append({"title": title, "purchase": rounded(purchase), "profit": rounded(profit),
                           "probability": probability, "reason": "Rientra nei limiti e nel margine richiesto"})
            used += purchase
            net += profit
            by_category[category] += purchase
            seen.add(card["group"])
            continue
        excluded.append({"title": title, "reason": reason})
    return {"chosen": chosen, "excluded": excluded, "spent": rounded(used),
            "left": rounded(capital - used), "profit": rounded(net), "horizon": horizon}


def price_scenarios(session, mode, product_id, channel, price, purchase=Decimal(0), condition="used", market="IT"):
    result = []
    for multiplier in (Decimal(".9"), Decimal(1), Decimal("1.1")):
        ask = price * multiplier
        result.append({"ask": rounded(ask),
                       "margin": margin(ask, purchase, *selling_costs(mode, channel)),
                       "liquidity": liquidity(session, mode, product_id, channel, ask,
                                              condition=condition, market=market)})
    return result


def component_estimates(session, mode, components, channel, horizon, costs, target):
    products = catalog(session, mode)
    lines, total, known = [], Decimal(0), 0
    measured_net, measured_quantity = Decimal(0), Decimal(0)
    uncertain = Decimal(0)
    demand_used = Counter()
    for item in components:
        product = products.get(item.get("product_id"))
        quantity = item.get("quantity")
        line = {**item, "name": product["name"] if product else item.get("title", "Da identificare")}
        line.update(unit=None, net=None, count=0, probability=None, unsold=None, horizon_net=None)
        if product and quantity and item.get("identification") in {"confirmed", "matched"}:
            condition = item.get("condition", "used")
            comparison = sales_features(session, mode, product["id"], condition, "IT", channel, now())
            low = dec(comparison["low"])
            estimation = margin(low, Decimal(0), *selling_costs(mode, channel)) if low is not None else None
            line["count"] = comparison["count"]
            if estimation:
                known += 1
                unit_net = max(Decimal(0), dec(estimation["net"]))
                net = unit_net * quantity
                observed = liquidity(session, mode, product["id"], channel, low,
                                     condition=condition)["probability"] if horizon == 30 else None
                probability = dec(observed)
                line.update(unit=rounded(low), net=rounded(net), probability=observed)
                total += net
                if probability is not None:
                    # Large lots cannot assume unlimited demand at the indicated price.
                    key = (product["id"], condition)
                    cap = Decimal(comparison["count"]) * horizon / 180
                    demand = min(Decimal(quantity) * probability, max(Decimal(0), cap - demand_used[key]))
                    demand_used[key] += demand
                    measured_net += unit_net * demand
                    measured_quantity += demand
                    line["unsold"] = rounded(Decimal(quantity) - demand)
                    line["horizon_net"] = rounded(unit_net * demand)
                else:
                    uncertain += net
        lines.append(line)
    return {"lines": lines, "known": known, "all_net": rounded(total - costs),
            "documented_net": rounded(measured_net - costs), "uncertain_net": rounded(uncertain),
            "measured_quantity": rounded(measured_quantity), "costs": rounded(costs),
            "max_offer": rounded(max(Decimal(0), measured_net - costs - target)),
            "max_if_all_sell": rounded(max(Decimal(0), total - costs - target))}


def check_build(products):
    attrs = {}
    for product in products:
        attrs.setdefault(product["category"], []).append(product["attrs"])
    checks = []
    cpu = next(iter(attrs.get("CPU", [])), {})
    gpu = next(iter(attrs.get("GPU", [])), {})
    board = next((item for item in attrs.get("Kit", []) if "socket" in item), {})
    case = next((item for item in attrs.get("Kit", []) if "psu_w" in item), {})

    def add(label, left, right, compatible):
        status = "da verificare" if left is None or right is None else "compatibile" if compatible(left, right) else "incompatibile"
        checks.append({"label": label, "left": left, "right": right, "status": status})

    add("Socket", cpu.get("socket"), board.get("socket"), lambda a, b: a == b)
    add("RAM", board.get("ram"), board.get("ram_installed"), lambda a, b: a == b)
    add("Formato", board.get("form_factor"), case.get("form_factors"), lambda a, b: a in b)
    add("Lunghezza GPU", gpu.get("gpu_mm"), case.get("max_gpu_mm"), lambda a, b: a <= b)
    add("Alimentatore", gpu.get("min_psu_w"), case.get("psu_w"), lambda a, b: a <= b)
    if len(attrs.get("CPU", [])) > 1 or len(attrs.get("GPU", [])) > 1:
        checks.append({"label": "Numero di CPU e GPU", "left": None, "right": None, "status": "incompatibile"})
    return checks


def request_matches(session, mode, cards):
    matches = []
    today = now().astimezone(ROME).date().isoformat()
    for request in latest(session, mode, "request"):
        row = request.data
        if row["expires"] < today:
            continue
        found, seen = [], set()
        for card in cards:
            offer = card["offer"].data
            if card["group"] in seen or offer["status"] != "available" or offer["condition"] not in row["conditions"]:
                continue
            if row.get("area") and clean(row["area"]) != clean(offer.get("area", "")):
                continue
            comparison = card["assessment"]
            matches_product = comparison["matching"]["product_id"] == row["product_id"]
            purchase = dec(comparison["purchase"])
            quantity = offer.get("quantity")
            if matches_product and purchase is not None and purchase <= dec(row["budget"]) and quantity != 0:
                found.append(card)
                seen.add(card["group"])
        matches.append({"request": request, "offers": found})
    return matches


def transition_inventory(session, mode, identifier, state, changes):
    prior = events(session, mode, "inventory", source="local", external_id=identifier)
    old = prior[-1].data if prior else {}
    allowed = {
        None: {"purchased"}, "purchased": {"verified", "returned"}, "verified": {"listed", "returned"},
        "listed": {"repriced", "sold", "returned"}, "repriced": {"listed", "repriced", "sold", "returned"},
        "sold": {"returned"}, "returned": {"verified"},
    }
    if state not in allowed.get(old.get("state"), set()):
        raise ValueError("Passaggio di stato non valido")
    data = {**old, **changes, "state": state}
    if state == "sold":
        costs = sum((dec(data.get(key)) or Decimal(0) for key in ("purchase", "fees", "outbound", "extras")), Decimal(0))
        data["realized_profit"] = rounded(dec(data["sale_price"]) - costs)
    elif state == "returned":
        data["realized_profit"] = None
    return record(session, mode, "inventory", "local", identifier, data, identity=uuid4().hex)


def draft_from_inventory(item, channel, price, title="", description=""):
    return {"item": item.external_id, "channel": channel, "title": title or item.data["title"],
            "description": description or item.data.get("specs", ""), "price": price,
            "currency": "EUR", "publisher": CHANNELS[channel].seller, "state": "draft"}


def market_changes(session, mode, days=30):
    current = now()
    boundary = current - timedelta(days=days)
    start = boundary - timedelta(days=days)
    result = []
    sales = latest(session, mode, "sale", cutoff=current)
    addresses = {item.source for item in sales}
    offers = events(session, mode, "offer", cutoff=current)
    addresses.update(item.source for item in offers)
    for source in sorted(addresses):
        before, after = Counter(), Counter()
        dates = set()
        previous_prices = {}
        previous_listings = set()
        stock = (set(), set())
        for item in offers:
            if item.source != source:
                continue
            moment = utc(item.known_at)
            if moment >= start:
                index = int(moment >= boundary)
                bucket = (before, after)[index]
                dates.add(index)
                if item.external_id not in previous_listings:
                    bucket["new"] += 1
                available = item.data["status"] == "available"
                if available:
                    stock[index].add(item.external_id)
                else:
                    stock[index].discard(item.external_id)
                prior = previous_prices.get(item.external_id)
                if item.data["price"] is not None and prior is not None and prior != (item.data["price"], item.data["currency"]):
                    bucket["price"] += 1
            previous_listings.add(item.external_id)
            if item.data["price"] is not None:
                previous_prices[item.external_id] = (item.data["price"], item.data["currency"])
        for item in sales:
            if item.source != source or item.data["status"] != "sold" or not item.data["verified"]:
                continue
            moment = utc(item.effective_at)
            if start <= moment <= current:
                (after if moment >= boundary else before)["sold"] += item.data["quantity"]
        before["stock"], after["stock"] = map(len, stock)
        result.append({"source": source, "before": before, "after": after, "days": days,
                       "status": "Copertura insufficiente" if dates != {0, 1} else
                       "Volume insufficiente" if min(before["sold"], after["sold"]) < 3 else
                       "Variazione vendite" if abs(after["sold"] - before["sold"]) >= max(3, before["sold"] * 0.3)
                       else "Nessun segnale oltre soglia"})
    return result
