from datetime import timedelta
from uuid import uuid4

from .catalog import catalog, clean
from .channels import CHANNELS
from .db import now
from .ingest import amount
from .store import digest, enqueue, latest, record, utc

MARKETPLACES = tuple(source for source, entry in CHANNELS.items() if entry.search)
DEFAULT_INTERVAL = 60
MIN_INTERVAL = 15
MAX_INTERVAL = 10080


def watches(session, mode):
    return {row.external_id: row for row in latest(session, mode, "product_watch")}


def save_watch(session, mode, product_id, sources, minutes, *,
               query="", max_cost="", min_profit="", signals=False, enabled=True):
    product = catalog(session, mode).get(product_id)
    if product is None:
        raise ValueError("Scegli un prodotto del catalogo")
    selected = sorted(set(sources))
    if not selected or set(selected) - set(MARKETPLACES):
        raise ValueError("Seleziona almeno un marketplace valido")
    if not MIN_INTERVAL <= minutes <= MAX_INTERVAL:
        raise ValueError("Intervallo consentito da 15 a 10.080 minuti")
    term = " ".join((query or product.get("search_query") or product["name"]).split())
    if not 2 <= len(term) <= 120:
        raise ValueError("La ricerca deve contenere da 2 a 120 caratteri")
    data = {"query": term, "sources": selected, "minutes": minutes, "enabled": enabled,
            "max_cost": amount(max_cost), "min_profit": amount(min_profit), "signals": signals}
    current = watches(session, mode).get(product_id)
    if current is not None and current.data == data:
        return current.id
    return record(session, mode, "product_watch", "local", product_id, data, identity=uuid4().hex)


def set_enabled(session, mode, product_id, enabled):
    current = watches(session, mode).get(product_id)
    if current is None:
        raise ValueError("Monitoraggio non trovato")
    if current.data["enabled"] == enabled:
        return
    record(session, mode, "product_watch", "local", product_id,
           {**current.data, "enabled": enabled}, identity=uuid4().hex)


def period_slot(timestamp, minutes):
    return int(timestamp.timestamp()) // (60 * minutes)


def schedule_search(session, mode, timestamp, subscription, source):
    query = subscription.data["query"]
    minutes = subscription.data["minutes"]
    slot = period_slot(timestamp, minutes)
    token = digest(f"{subscription.id}:{source}:{clean(query)}:{minutes}:{slot}")[:32]
    return enqueue(session, mode, "discover", f"discover:{token}:start",
                   {"run": token, "query": query, "source": source, "cursor": None, "page": 1,
                    "watch_event": subscription.id})


def is_current(session, mode, subscription):
    if subscription.kind == "product_watch":
        current = watches(session, mode).get(subscription.external_id)
    else:
        current = next((row for row in latest(session, mode, "watch")
                        if row.source == subscription.source and row.external_id == subscription.external_id), None)
    return current is not None and current.id == subscription.id and current.data["enabled"]


def selection_for(session, mode, offer, assessment):
    selected = watches(session, mode)
    if not selected:
        return True, None
    product_id = assessment["matching"]["product_id"]
    item = selected.get(product_id)
    if item is None or not item.data["enabled"] or offer.source not in item.data["sources"]:
        return False, None
    return True, item.data


def save_signal(session, mode, offer, assessment, selection, minimum):
    if not selection or not selection["signals"] or offer.data["status"] == "unavailable":
        return
    if utc(offer.effective_at) < now() - timedelta(hours=24):
        return
    if offer.data["condition"] == "broken" or assessment["matching"]["type"] in {"part", "packaging", "accessory", "broken"}:
        return
    from .value import dec, eur

    purchase = dec(assessment.get("purchase"))
    limit = dec(selection.get("max_cost"))
    known_cost = purchase if purchase is not None else eur(offer.data, "price")
    if limit is not None and known_cost is not None and known_cost > limit:
        return
    best = next(iter(assessment["estimates"]), None)
    margin = best.get("margin") if best else None
    if offer.data["status"] == "available" and margin and best["confidence"] == "buona":
        return
    pending = []
    if offer.data["status"] != "available":
        pending.append("disponibilità da confermare")
    if purchase is None:
        pending.append("costo consegnato incompleto")
    if not margin:
        pending.append("margine non calcolabile")
    elif best["confidence"] != "buona":
        if dec(margin["profit"]) < minimum:
            return
        pending.append("pochi confronti pertinenti")
    record(session, mode, "monitor_signal", offer.source, offer.external_id, {
        "title": offer.data["title"], "offer_id": offer.id, "reasons": pending,
        "price": offer.data.get("price"), "currency": offer.data.get("currency"),
        "purchase": assessment.get("purchase"),
    }, identity="first-incomplete-observation")
