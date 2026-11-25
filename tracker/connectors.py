import os
import time
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from urllib.parse import quote, urlsplit
from uuid import uuid4

from .catalog import catalog, clean, match
from .channels import source_rules
from .db import Event, now
from .ingest import preview, write_rows
from .mail_alerts import MailError, fetch_messages, parse_message, settings as mail_settings
from .monitoring import is_current, period_slot, schedule_search, watches
from .store import digest, enqueue, events, latest, record


class FetchError(RuntimeError):
    def __init__(self, message, *, retry=True, delay=0):
        super().__init__(message)
        self.retry = retry
        self.delay = delay


def checked(response):
    if response.status_code in (401, 403):
        raise FetchError("Credenziali o accesso da verificare", retry=False)
    if response.status_code == 429 or response.status_code >= 500:
        delay = response.headers.get("Retry-After", "")
        raise FetchError(f"Errore temporaneo HTTP {response.status_code}", delay=int(delay) if delay.isdigit() else 0)
    if not response.is_success:
        raise FetchError(f"Risposta HTTP {response.status_code}", retry=False)
    try:
        body = response.json()
    except ValueError:
        raise FetchError("Risposta JSON non valida") from None
    if not isinstance(body, dict):
        raise FetchError("Risposta JSON inattesa")
    return body


@dataclass
class Page:
    rows: list
    next_cursor: str | None = None


class EbayBrowse:
    supports_refresh = True

    def __init__(self, client, key, secret):
        self.http = client
        self.app = (key, secret)
        self.base = "https://api.ebay.com"
        self.token = None
        self.expires = 0

    def headers(self):
        if not self.token or self.expires < time.monotonic():
            response = self.http.post(
                self.base + "/identity/v1/oauth2/token", auth=self.app,
                data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
            )
            body = checked(response)
            self.token = body.get("access_token")
            if not self.token:
                raise FetchError("Token Browse assente", retry=False)
            self.expires = time.monotonic() + max(0, int(body.get("expires_in", 300)) - 20)
        return {"Authorization": f"Bearer {self.token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_IT",
                "X-EBAY-C-ENDUSERCTX": "contextualLocation=country%3DIT"}

    @staticmethod
    def normalize(item):
        price = item.get("price") or {}
        shipping = [option["shippingCost"] for option in item.get("shippingOptions", [])
                    if option.get("shippingCost", {}).get("currency") == price.get("currency")]
        shipping = min(shipping, key=lambda value: float(value["value"])) if shipping else {}
        condition = str(item.get("conditionId", ""))
        if condition in {"1000", "1500", "1750"}:
            condition = "new"
        elif condition in {"2000", "2010", "2020", "2030", "2500"}:
            condition = "refurbished"
        elif condition in {"2750", "2990", "3000", "3010", "4000", "5000", "6000"}:
            condition = "used"
        else:
            condition = "broken" if condition == "7000" else "unknown"
        availability = {entry.get("estimatedAvailabilityStatus") for entry in item.get("estimatedAvailabilities", [])}
        status = "available"
        if "OUT_OF_STOCK" in availability and not ({"IN_STOCK", "LIMITED_STOCK"} & availability):
            status = "unavailable"
        return {"external_id": item.get("itemId"), "title": item.get("title"), "condition": condition,
                "description": item.get("shortDescription", ""), "price": price.get("value"), "shipping": shipping.get("value"),
                "currency": price.get("currency", "EUR"), "status": status, "url": item.get("itemWebUrl", ""),
                "image_url": (item.get("image") or {}).get("imageUrl", ""), "published_at": item.get("itemCreationDate", ""),
                "market": "IT", "sale_format": "fixed"}

    def discover(self, query, cursor):
        offset = int(cursor or 0)
        response = self.http.get(
            self.base + "/buy/browse/v1/item_summary/search", headers=self.headers(),
            params={"q": query, "limit": 50, "offset": offset, "filter": "deliveryCountry:IT"},
        )
        body = checked(response)
        return Page([self.normalize(item) for item in body.get("itemSummaries", [])],
                    str(offset + 50) if body.get("next") and offset + 50 < 10000 else None)

    def refresh(self, item_id):
        response = self.http.get(
            self.base + "/buy/browse/v1/item/" + quote(item_id, safe=""), headers=self.headers()
        )
        if response.status_code == 404:
            return None
        return self.normalize(checked(response))


class JsonFeed:
    supports_refresh = False

    def __init__(self, client, url, token=None, kind="offer"):
        if urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
            raise FetchError("Il feed richiede un URL HTTPS", retry=False)
        self.http, self.url, self.token, self.kind = client, url, token, kind

    def discover(self, query, cursor):
        parameters = {"q": query}
        if cursor is not None:
            parameters["cursor"] = cursor
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        body = checked(self.http.get(self.url, params=parameters, headers=headers))
        rows = body.get("items")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise FetchError("Il feed richiede items come elenco di oggetti")
        next_cursor = body.get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, (str, int)):
            raise FetchError("Cursore feed non valido")
        return Page(rows, str(next_cursor) if next_cursor is not None else None)


def direct_connection(source):
    suffix = source.upper().replace("-", "_")
    if os.getenv(f"TRACKER_FEED_{suffix}_URL"):
        return "Feed JSON"
    if source == "ebay" and os.getenv("EBAY_CLIENT_ID") and os.getenv("EBAY_CLIENT_SECRET"):
        return "Browse API"
    if source == "amazon" and os.getenv("AMAZON_CREATORS_TRACKER_APPROVED") == "1" and all(
            os.getenv(key) for key in ("AMAZON_CREATORS_ID", "AMAZON_CREATORS_SECRET", "AMAZON_CREATORS_TAG")):
        return "Creators API"
    if source == "aliexpress" and all(
            os.getenv(key) for key in ("ALIEXPRESS_TOP_KEY", "ALIEXPRESS_TOP_SECRET", "ALIEXPRESS_TRACKING_ID")):
        return "Affiliate API"
    return None


def connections(source):
    from .search_index import search_key

    return [value for value in (direct_connection(source) or ("Indice Brave" if search_key(source) else None),
            "Avvisi email" if mail_settings(source) else None) if value]


@lru_cache(maxsize=32)
def adapter(client, source):
    suffix = source.upper().replace("-", "_")
    if url := os.getenv(f"TRACKER_FEED_{suffix}_URL"):
        kind = os.getenv(f"TRACKER_FEED_{suffix}_KIND", "offer")
        if kind not in {"offer", "sale"}:
            raise FetchError("Tipo di feed non valido", retry=False)
        return JsonFeed(client, url, os.getenv(f"TRACKER_FEED_{suffix}_TOKEN"), kind)
    connection = direct_connection(source)
    if connection == "Browse API":
        return EbayBrowse(client, os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"])
    if connection == "Creators API":
        from .retail import AmazonCreators
        return AmazonCreators(client, os.environ["AMAZON_CREATORS_ID"], os.environ["AMAZON_CREATORS_SECRET"],
                              os.environ["AMAZON_CREATORS_TAG"])
    if connection == "Affiliate API":
        from .retail import AliExpressAffiliate
        return AliExpressAffiliate(client, os.environ["ALIEXPRESS_TOP_KEY"], os.environ["ALIEXPRESS_TOP_SECRET"],
                                   os.environ["ALIEXPRESS_TRACKING_ID"])
    from .search_index import BraveWebSearch, search_key
    if key := search_key(source):
        return BraveWebSearch(client, source, key)
    raise FetchError("Connettore automatico da configurare", retry=False)


def watch(session, mode, query, source, minutes=60):
    identifier = digest(f"{source}\n{query.casefold()}")[:24]
    data = {"query": query, "minutes": minutes, "enabled": True}
    current = next((row for row in latest(session, mode, "watch")
                    if row.source == source and row.external_id == identifier), None)
    if current is None or current.data != data:
        record(session, mode, "watch", source, identifier, data, identity=uuid4().hex)
    return identifier


def active_subscriptions(session, mode):
    subscriptions = [(row, (row.source,), True) for row in latest(session, mode, "watch")]
    subscriptions.extend((row, row.data["sources"], False) for row in watches(session, mode).values())
    return [(row, sources, legacy) for row, sources, legacy in subscriptions if row.data["enabled"]]


def schedule(session, mode, timestamp=None):
    if mode != "real":
        return
    from .search_index import search_key

    timestamp = timestamp or now()
    rules = source_rules(session, mode)
    inboxes = {}
    for row, sources, legacy in active_subscriptions(session, mode):
        for source in sources:
            if not rules.get(source, {}).get("history"):
                continue
            indexed = not legacy and row.data["signals"] and search_key(source)
            if direct_connection(source) or indexed:
                schedule_search(session, mode, timestamp, row, source)
            if mail_settings(source):
                minutes = row.data["minutes"]
                inboxes[source] = min(minutes, inboxes.get(source, minutes))
    for source, minutes in inboxes.items():
        run = f"mail:{source}:{minutes}:{period_slot(timestamp, minutes)}"
        enqueue(session, mode, "mail", run, {"source": source, "run": run, "page": 1})


def collect_mail(session, mode, job):
    source = job.data["source"]
    config = mail_settings(source)
    subscriptions = [(item, legacy) for item, sources, legacy in active_subscriptions(session, mode)
                     if source in sources]
    if config is None or not subscriptions:
        return

    checkpoint_id = digest("\n".join((config.host, config.user, config.folder, config.sender)))
    snapshots = events(session, mode, "mail_cursor", source=source, external_id=checkpoint_id)
    checkpoint = snapshots[-1].data if snapshots else {}
    try:
        validity, position, messages, more = fetch_messages(config, checkpoint)
    except MailError as error:
        raise FetchError(str(error), retry=error.retry) from error
    timestamp = now()
    rows = []
    products = catalog(session, mode)
    selected = {item.external_id for item, legacy in subscriptions if not legacy}
    terms = [set(clean(item.data["query"]).split()) for item, legacy in subscriptions if legacy]
    existing = {row.external_id: row.data for row in latest(session, mode, "offer") if row.source == source}
    for _, raw in messages:
        for row in parse_message(source, raw, config.sender, timestamp):
            if row["external_id"] in existing and existing[row["external_id"]].get("checked_by") != "mail":
                continue
            product_id = match(row, products)["product_id"]
            if product_id not in selected and not any(term <= set(clean(row["title"]).split()) for term in terms):
                continue
            rows.append((len(rows) + 1, row))
    valid, errors = preview(rows, {}, "offer")
    write_rows(session, mode, source, "offer", valid, errors, digest(job.key))
    data = {"validity": validity, "uid": position}
    if checkpoint != data:
        record(session, mode, "mail_cursor", source, checkpoint_id, data)
    record(session, mode, "collection", source, job.key, {
        "query": "Avvisi email", "page": job.data["page"], "received": len(rows), "rejected": len(errors),
        "messages": len(messages), "status": "partial" if errors else "success",
    })
    if more:
        enqueue(session, mode, "mail", f"{job.data['run']}:{validity}:{position}",
                {**job.data, "page": job.data["page"] + 1}, timestamp + timedelta(seconds=3))


def collect(session, mode, job, client):
    payload = job.data
    source = payload["source"]
    subscription = None
    if job.kind == "discover" and payload.get("watch_event"):
        subscription = session.get(Event, payload["watch_event"])
        if subscription is None or not is_current(session, mode, subscription):
            return
    if not source_rules(session, mode).get(source, {}).get("history"):
        raise FetchError("Conservazione dello storico da autorizzare nella fonte", retry=False)
    if job.kind == "mail":
        collect_mail(session, mode, job)
        return
    connector = adapter(client, source)
    timestamp = now()
    if job.kind == "refresh":
        if not connector.supports_refresh:
            return
        entry = next((row for row in latest(session, mode, "offer")
                      if row.source == source and row.external_id == payload["id"]), None)
        if entry is None:
            return
        row = connector.refresh(payload["id"])
        if row is None:
            row = {**entry.data, "external_id": entry.external_id, "status": "unknown"}
        else:
            row = {**entry.data, **row}
        row["checked_by"] = "detail"
        row["observed_at"] = timestamp.isoformat()
        valid, errors = preview([(1, row)], {}, "offer")
        if errors:
            raise FetchError("Risposta di ricontrollo non valida")
        write_rows(session, mode, source, "offer", valid, errors, digest(job.key))
        evaluations = [item for item in latest(session, mode, "valuation")
                       if item.source == source and item.external_id == payload["id"]]
        best = next(iter(evaluations[0].data["estimates"]), None) if evaluations else None
        near_threshold = best and best["margin"] and abs(
            float(best["margin"]["profit"]) - float(os.getenv("TRACKER_MIN_PROFIT", "25"))
        ) <= 15
        delay = 12 if row["status"] != "available" else 1 if near_threshold else 3
        enqueue(session, mode, "refresh", f"refresh:after:{job.id}", payload, timestamp + timedelta(hours=delay))
        return
    cursor = payload.get("cursor")
    kind = getattr(connector, "kind", "offer")
    if kind == "search_index" and (not subscription or subscription.kind != "product_watch" or not subscription.data["signals"]):
        return
    page = connector.discover(payload["query"], cursor)
    details = {"query": payload["query"], "page": payload["page"], "received": len(page.rows)}
    if kind == "search_index":
        from .search_index import save_index_results

        accepted = save_index_results(session, mode, source, subscription.external_id, catalog(session, mode), page.rows)
        details.update(transport="Indice Brave", matched=accepted, rejected=0, status="success")
        valid = []
    else:
        rows = list(enumerate(page.rows, start=1))
        if kind == "offer":
            rows = [(index, {**row, "observed_at": row.get("observed_at") or timestamp.isoformat()}) for index, row in rows]
        valid, errors = preview(rows, {}, kind)
        write_rows(session, mode, source, kind, valid, errors, digest(job.key), refresh=False)
        details.update(rejected=len(errors), status="partial" if errors else "success")
    if connector.supports_refresh:
        hour = int(timestamp.timestamp()) // (60 * 60 * 12)
        for _, (data, _, _, _) in valid:
            identifier = data["external_id"]
            enqueue(session, mode, "refresh", f"refresh:seed:{source}:{digest(identifier)[:24]}:{hour}",
                    {"source": source, "id": identifier}, timestamp + timedelta(hours=3))
    record(session, mode, "collection", source, f"{payload['run']}:{payload['page']}", details)
    if page.next_cursor is not None and page.next_cursor != cursor and payload["page"] < 40:
        key = f"discover:{payload['run']}:{digest(page.next_cursor)[:24]}"
        enqueue(session, mode, "discover", key,
                {**payload, "cursor": page.next_cursor, "page": payload["page"] + 1},
                timestamp + timedelta(seconds=3))
