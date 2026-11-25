import os
from html import unescape

from .catalog import clean, match
from .channels import CHANNELS
from .connectors import FetchError, Page, checked
from .ingest import clean_text
from .mail_alerts import canonical_listing
from .store import record

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
SEARCH_DOMAINS = {
    "ebay": "ebay.it",
    "subito": "subito.it",
    "vinted": "vinted.it",
    "amazon": "amazon.it",
    "aliexpress": "aliexpress.com",
    "wallapop": "wallapop.com",
    "facebook": "facebook.com",
}


def search_key(source):
    enabled = {item.strip().casefold() for item in os.getenv("TRACKER_BRAVE_SOURCES", "").split(",")}
    if source in SEARCH_DOMAINS and source in enabled and os.getenv("TRACKER_BRAVE_STORAGE_ALLOWED") == "1":
        return os.getenv("TRACKER_BRAVE_API_KEY", "").strip()
    return ""


class BraveWebSearch:
    kind = "search_index"
    supports_refresh = False

    def __init__(self, client, source, key):
        self.http, self.source, self.key = client, source, key
        try:
            self.page_limit = int(os.getenv("TRACKER_BRAVE_MAX_PAGES", "1"))
        except ValueError:
            raise FetchError("TRACKER_BRAVE_MAX_PAGES deve essere tra 1 e 10", retry=False) from None
        if not 1 <= self.page_limit <= 10:
            raise FetchError("TRACKER_BRAVE_MAX_PAGES deve essere tra 1 e 10", retry=False)

    def discover(self, query, cursor):
        offset = int(cursor or "0")
        keywords = clean(query)
        if not keywords:
            raise FetchError("La ricerca nell’indice richiede parole o codici del prodotto", retry=False)
        if self.source == "facebook":
            keywords += " marketplace"
        body = checked(self.http.get(ENDPOINT, headers={"X-Subscription-Token": self.key}, params={
            "q": f"{keywords} site:{SEARCH_DOMAINS[self.source]}",
            "country": "IT", "search_lang": "it", "ui_lang": "it-IT",
            "offset": offset, "count": 20, "result_filter": "web",
            "text_decorations": "false", "spellcheck": "false",
        }))
        details = body.get("query", {})
        web = body.get("web", {})
        if not isinstance(details, dict) or not isinstance(web, dict) or not ("query" in body or "web" in body):
            raise FetchError("Risposta Brave non valida")
        results = web.get("results", [])
        if not isinstance(results, list):
            raise FetchError("Elenco risultati Brave non valido")
        next_cursor = str(offset + 1) if details.get("more_results_available") and offset + 1 < self.page_limit else None
        return Page(results, next_cursor)


def save_index_results(session, mode, source, product_id, products, rows):
    seen = set()
    for hit in rows:
        if not isinstance(hit, dict) or not isinstance(hit.get("url"), str) or not isinstance(hit.get("title"), str):
            continue
        if not (link := canonical_listing(source, hit["url"])):
            continue
        identifier, url = link
        title = " ".join(clean_text(unescape(hit["title"])).split())
        publisher = CHANNELS[source].name.casefold()
        if " | " in title:
            name, suffix = title.rsplit(" | ", 1)
            if suffix.casefold() in {publisher, SEARCH_DOMAINS[source], "ebay", "amazon", "facebook"}:
                title = name
        if not title or match({"title": title}, products)["product_id"] != product_id or identifier in seen:
            continue
        seen.add(identifier)
        title = title[:512]
        record(session, mode, "index_result", source, identifier,
               {"title": title, "url": url, "product_id": product_id, "provider": "brave_web_search"})
        record(session, mode, "monitor_signal", source, identifier, {
            "title": title, "url": url, "price": None, "purchase": None,
            "reasons": ["risultato indicizzato", "disponibilità e costo da verificare"],
        }, identity="first-incomplete-observation")
    return len(seen)
