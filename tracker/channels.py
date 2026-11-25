import os
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote
from uuid import uuid4

from .store import latest, record


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    search: str
    seller: str
    method: str
    fee: Decimal | None = None

    def search_url(self, term):
        return self.search.replace("{query}", quote(term, safe=""))


CHANNELS = {item.id: item for item in (
    Channel("ebay", "eBay Italia", "https://www.ebay.it/sch/i.html?_nkw={query}", "https://www.ebay.it/sl/sell",
            "Browse API o feed", Decimal("0.12")),
    Channel("subito", "Subito", "https://www.subito.it/annunci-italia/vendita/usato/?q={query}",
            "https://www.subito.it/account/nuovo-annuncio", "Avvisi email o feed autorizzato", Decimal("0")),
    Channel("vinted", "Vinted", "https://www.vinted.it/catalog?search_text={query}",
            "https://www.vinted.it/items/new", "Feed autorizzato o import"),
    Channel("amazon", "Amazon Italia", "https://www.amazon.it/s?k={query}",
            "https://sellercentral.amazon.it/", "Creators API o feed autorizzato"),
    Channel("aliexpress", "AliExpress", "https://www.aliexpress.com/w/wholesale-{query}.html",
            "https://seller.aliexpress.com/", "Affiliate API o feed autorizzato"),
    Channel("wallapop", "Wallapop", "https://it.wallapop.com/app/search?keywords={query}",
            "https://it.wallapop.com/", "Feed autorizzato o import"),
    Channel("facebook", "Facebook Marketplace", "https://www.facebook.com/marketplace/search/?query={query}",
            "https://www.facebook.com/marketplace/create/item", "Feed autorizzato o import"),
    Channel("local", "Import locale", "", "", "CSV o JSON"),
)}


def source_rules(session, mode, cutoff=None):
    return {item.source: item.data for item in latest(session, mode, "source_policy", cutoff=cutoff)}


def set_source(session, mode, source, *, history=True, training=False, note=""):
    record(session, mode, "source_policy", source, source,
           {"history": bool(history), "training": bool(training and history), "note": note[:400]},
           identity=uuid4().hex)


def fee_for(source, mode):
    if mode == "demo" and source in CHANNELS:
        return CHANNELS[source].fee
    value = os.getenv(f"TRACKER_FEE_{source.upper().replace('-', '_')}")
    if value is None:
        return None
    rate = Decimal(value)
    if not rate.is_finite() or not 0 <= rate <= 1:
        raise ValueError("Commissione fuori intervallo (0–1)")
    return rate
