import json
import os
from contextlib import suppress
from pathlib import Path

from sqlalchemy import select

from .catalog import catalog
from .channels import source_rules
from .connectors import connections
from .db import Event, now
from .monitoring import period_slot, watches
from .search_index import SEARCH_DOMAINS, save_index_results
from .store import digest, record


def enabled():
    return os.getenv("TRACKER_CODEX_DISCOVERY") == "1"


def targets(session, clock, *, previous=False):
    policies = source_rules(session, "real")
    items = {}
    for product, subscription in watches(session, "real").items():
        watch = subscription.data
        if not watch["enabled"] or not watch["signals"]:
            continue
        slot = period_slot(clock, watch["minutes"])
        for source in watch["sources"]:
            if source not in SEARCH_DOMAINS or connections(source):
                continue
            if not policies.get(source, {"history": True})["history"]:
                continue
            periods = (slot, slot - 1) if previous else (slot,)
            for period in periods:
                key = digest(f"codex:{subscription.id}:{source}:{period}")[:32]
                items[key] = {"id": key, "product": product, "source": source,
                              "domain": SEARCH_DOMAINS[source], "query": watch["query"]}
    if not items:
        return items
    done = set(session.scalars(select(Event.external_id).where(
        Event.mode == "real", Event.kind == "collection", Event.external_id.in_(items),
    )))
    return {key: item for key, item in items.items() if key not in done}


def synchronize(sessions, directory=None, clock=None):
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
            if filename.stat().st_size > 128_000:
                continue
            hits = json.loads(filename.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, PermissionError, UnicodeError, ValueError):
            continue
        if not isinstance(hits, list) or len(hits) > 40:
            continue
        target = pending[key]
        source = target["source"]
        with sessions.begin() as session:
            if key in targets(session, clock, previous=True):
                matched = save_index_results(session, "real", source, target["product"],
                                             catalog(session, "real"), hits, provider="codex_web_search")
                record(session, "real", "collection", source, key,
                       {"transport": "Indice web Codex", "status": "success", "query": target["query"],
                        "received": len(hits), "matched": matched, "rejected": 0, "page": 1})
        with suppress(PermissionError):
            filename.unlink(missing_ok=True)
    with sessions() as session:
        ready = targets(session, clock)
    manifest = directory / "requests.json"
    data = json.dumps({"items": list(ready.values())}, ensure_ascii=False, indent=2)
    if not manifest.exists() or manifest.read_text(encoding="utf-8") != data:
        scratch = directory / f"requests-{os.getpid()}.tmp"
        scratch.write_text(data, encoding="utf-8")
        scratch.replace(manifest)
