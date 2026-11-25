import json
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

from .channels import CHANNELS, source_rules
from .db import now
from .ingest import preview, write_rows
from .store import digest, enqueue, latest, record


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


def configured(source):
    suffix = source.upper().replace("-", "_")
    if os.getenv(f"TRACKER_FEED_{suffix}_URL"):
        return True
    return source == "ebay" and bool(os.getenv("EBAY_CLIENT_ID") and os.getenv("EBAY_CLIENT_SECRET"))


def adapter(client, source):
    suffix = source.upper().replace("-", "_")
    if url := os.getenv(f"TRACKER_FEED_{suffix}_URL"):
        kind = os.getenv(f"TRACKER_FEED_{suffix}_KIND", "offer")
        if kind not in {"offer", "sale"}:
            raise FetchError("Tipo di feed non valido", retry=False)
        return JsonFeed(client, url, os.getenv(f"TRACKER_FEED_{suffix}_TOKEN"), kind)
    if configured(source) and source == "ebay":
        return EbayBrowse(client, os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"])
    raise FetchError("Connettore automatico da configurare", retry=False)


def watch(session, mode, query, source, minutes=60):
    identifier = digest(f"{source}\n{query.casefold()}")[:24]
    record(session, mode, "watch", source, identifier,
           {"query": query, "minutes": minutes, "enabled": True}, identity=uuid4().hex)
    return identifier


def schedule(session, mode, timestamp=None):
    if mode != "real":
        return
    timestamp = timestamp or now()
    rules = source_rules(session, mode)
    for row in latest(session, mode, "watch"):
        if not row.data["enabled"] or not configured(row.source) or not rules.get(row.source, {}).get("history"):
            continue
        bucket = int(timestamp.timestamp()) // (60 * row.data["minutes"])
        run = f"{row.external_id}:{bucket}"
        enqueue(session, mode, "discover", f"discover:{run}:start",
                {"run": run, "query": row.data["query"], "source": row.source, "cursor": None, "page": 1})


def collect(session, mode, job, client):
    payload = job.data
    source = payload["source"]
    if not source_rules(session, mode).get(source, {}).get("history"):
        raise FetchError("Conservazione dello storico da autorizzare nella fonte", retry=False)
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
    page = connector.discover(payload["query"], cursor)
    kind = getattr(connector, "kind", "offer")
    rows = list(enumerate(page.rows, start=1))
    if kind == "offer":
        rows = [(index, {**row, "observed_at": row.get("observed_at") or timestamp.isoformat()}) for index, row in rows]
    valid, errors = preview(rows, {}, kind)
    write_rows(session, mode, source, kind, valid, errors, digest(job.key), refresh=False)
    if connector.supports_refresh:
        hour = int(timestamp.timestamp()) // (60 * 60 * 12)
        for _, (data, _, _, _) in valid:
            identifier = data["external_id"]
            enqueue(session, mode, "refresh", f"refresh:seed:{source}:{digest(identifier)[:24]}:{hour}",
                    {"source": source, "id": identifier}, timestamp + timedelta(hours=3))
    record(session, mode, "collection", source, f"{payload['run']}:{payload['page']}",
           {"query": payload["query"], "page": payload["page"], "received": len(rows), "rejected": len(errors),
            "status": "partial" if errors else "success"})
    if page.next_cursor is not None and page.next_cursor != cursor and payload["page"] < 40:
        key = f"discover:{payload['run']}:{digest(page.next_cursor)[:24]}"
        enqueue(session, mode, "discover", key,
                {**payload, "cursor": page.next_cursor, "page": payload["page"] + 1},
                timestamp + timedelta(seconds=3))
