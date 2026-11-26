import json
import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from .availability import CHECK_SOURCES, STATUSES, listing_availability, save_listing_availability
from .catalog import catalog
from .channels import source_rules
from .connectors import connections, direct_connection
from .db import Event, now
from .mail_alerts import canonical_listing
from .monitoring import period_slot, watches
from .search_index import SEARCH_DOMAINS, index_settings, save_index_results
from .store import digest, record, utc


CHECK_AGE = timedelta(days=7)
CHECK_LIMIT = 5
CHECK_BATCH = "listing_availability_batch"


def enabled():
    return os.getenv("TRACKER_CODEX_DISCOVERY") == "1"


def configured():
    return enabled() or os.getenv("TRACKER_CODEX_BRIDGE") == "1"


def checks_indexed_listings(source):
    return (source in CHECK_SOURCES and not direct_connection(source)
            and index_settings(source) is not None)


def targets(session, clock, *, previous=False):
    policies = source_rules(session, "real")
    items = {}
    for product, subscription in watches(session, "real").items():
        watch = subscription.data
        if not watch["enabled"] or not watch["signals"]:
            continue
        slot = period_slot(clock, watch["minutes"])
        for source in watch["sources"]:
            if source not in SEARCH_DOMAINS:
                continue
            checks_only = checks_indexed_listings(source)
            if not policies.get(source, {"history": not checks_only})["history"]:
                continue
            if connections(source) and not checks_only:
                continue
            periods = (slot, slot - 1) if previous else (slot,)
            task = "codex-checks" if checks_only else "codex"
            for period in periods:
                key = digest(f"{task}:{subscription.id}:{source}:{period}")[:32]
                items[key] = {"id": key, "product": product, "source": source,
                              "domain": SEARCH_DOMAINS[source], "query": watch["query"],
                              "period": period * watch["minutes"] * 60}
                if checks_only:
                    items[key]["check_only"] = True
    if not items:
        return items
    done = set(session.scalars(select(Event.external_id).where(
        Event.mode == "real", Event.kind.in_(("collection", CHECK_BATCH)), Event.external_id.in_(items),
    )))
    return {key: item for key, item in items.items() if key not in done}


def recent_listings(session, target, clock):
    rows = session.scalars(select(Event).where(
        Event.mode == "real", Event.kind == "index_result", Event.source == target["source"],
        Event.known_at >= clock - CHECK_AGE, Event.data["product_id"].as_string() == target["product"],
    ).order_by(Event.known_at, Event.id))
    return {item.external_id: item for item in rows}


def apply_checks(session, target, hits, checks, identity, observed_at, clock):
    source = target["source"]
    listings = recent_listings(session, target, clock)
    period = datetime.fromtimestamp(target["period"], tz=UTC)
    seen = set()
    for item in [*checks, *hits]:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        observed = item.get("status")
        if not isinstance(observed, str) or observed not in STATUSES:
            continue
        link = canonical_listing(source, item["url"])
        if not link or link[0] not in listings or link[0] in seen:
            continue
        identifier, url = link
        seen.add(identifier)
        save_listing_availability(session, source, identifier, url, observed, identity,
                                  period, observed_at, clock)
    return len(seen)


def synchronize(sessions, directory=None, clock=None, *, lease_seconds=None):
    if not enabled():
        return
    directory = directory or Path("data/codex-index")
    inbox = directory / "results"
    inbox.mkdir(parents=True, exist_ok=True)
    clock = clock or now()
    with sessions() as session:
        pending = targets(session, clock, previous=True)
    for filename in inbox.glob("*.json"):
        key = filename.stem
        if key not in pending:
            # Completed, expired, or no longer selected. Keep unrelated files untouched.
            if len(key) == 32 and all(char in "0123456789abcdef" for char in key):
                with suppress(PermissionError):
                    filename.unlink(missing_ok=True)
            continue
        try:
            info = filename.stat()
            if info.st_size > 128_000:
                continue
            payload = json.loads(filename.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, PermissionError, UnicodeError, ValueError):
            continue
        target = pending[key]
        source = target["source"]
        availability_only = target.get("check_only", False)
        hits = payload.get("hits", False) if isinstance(payload, dict) else payload
        checks_only = availability_only or (isinstance(payload, dict) and hits is None)
        if checks_only:
            hits = []
        checks = payload.get("checks", []) if isinstance(payload, dict) else []
        if not isinstance(hits, list) or len(hits) > 40 or not isinstance(checks, list) or len(checks) > CHECK_LIMIT:
            continue
        if checks_only and (source not in CHECK_SOURCES or not checks):
            continue
        with sessions.begin() as session:
            if key in targets(session, clock, previous=True):
                if not checks_only:
                    matched = save_index_results(session, "real", source, target["product"],
                                                 catalog(session, "real"), hits, provider="codex_web_search",
                                                 collection_run=key)
                    record(session, "real", "collection", source, key,
                           {"transport": "Indice web Codex", "status": "success", "query": target["query"],
                            "received": len(hits), "matched": matched, "rejected": 0, "page": 1})
                if source in CHECK_SOURCES and (hits or checks):
                    observed_at = min(clock, datetime.fromtimestamp(info.st_mtime, tz=UTC))
                    identity = f"{key}:checks" if checks_only else key
                    applied = apply_checks(session, target, hits, checks, identity, observed_at, clock)
                    if availability_only and applied:
                        record(session, "real", CHECK_BATCH, source, key,
                               {"product_id": target["product"], "checked": applied,
                                "provider": "codex_web_search"}, identity=key)
        with suppress(PermissionError):
            filename.unlink(missing_ok=True)
    with sessions() as session:
        ready = targets(session, clock)
        previous_checks = listing_availability(session, "real") if ready else {}
        for target in ready.values():
            period = datetime.fromtimestamp(target.pop("period"), tz=UTC)
            source = target["source"]
            if source not in CHECK_SOURCES:
                continue
            candidates = []
            for identifier, item in recent_listings(session, target, clock).items():
                previous = previous_checks.get((source, identifier))
                if previous and (previous.status in {"sold", "unavailable"} or utc(previous.period) >= period):
                    continue
                candidates.append((utc(previous.checked_at) if previous else utc(item.known_at) - CHECK_AGE,
                                   identifier, item.data["url"]))
            target["checks"] = [url for _, _, url in sorted(candidates)[:CHECK_LIMIT]]
    manifest = directory / "requests.json"
    payload = {"items": [target for target in ready.values() if not target.get("check_only") or target["checks"]]}
    if lease_seconds is not None:
        payload["active_until"] = (clock + timedelta(seconds=lease_seconds)).isoformat()
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    with suppress(PermissionError):
        if not manifest.exists() or manifest.read_text(encoding="utf-8") != data:
            scratch = directory / f"requests-{os.getpid()}.tmp"
            scratch.write_text(data, encoding="utf-8")
            scratch.replace(manifest)
