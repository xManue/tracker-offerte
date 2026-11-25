from dataclasses import dataclass
from datetime import datetime

from .db import Event
from .store import latest, record


CHECK_SOURCES = {"subito", "vinted"}
CLOSED = {"sold", "reserved", "unavailable"}
STATUSES = CLOSED | {"available", "unknown"}
KIND = "listing_availability"


def listing_availability(session, mode):
    attempts = latest(session, mode, KIND)
    confirmed = {}
    if any(item.data["status"] == "unknown" for item in attempts):
        confirmed = {(item.source, item.external_id): item for item in latest(
            session, mode, KIND, where=Event.data["status"].as_string() != "unknown",
        )}
    availability = {}
    for item in attempts:
        key = (item.source, item.external_id)
        evidence = item
        previous = confirmed.get(key)
        if item.data["status"] == "unknown" and previous and previous.data["status"] in CLOSED:
            evidence = previous
        availability[key] = Availability(
            evidence.data["status"], datetime.fromisoformat(item.data["observed_at"]),
            datetime.fromisoformat(evidence.data["observed_at"]),
        )
    return availability


@dataclass(frozen=True)
class Availability:
    status: str
    checked_at: datetime
    evidence_at: datetime


def save_listing_availability(session, source, identifier, url, status, request_id, period, observed_at, clock):
    record(session, "real", KIND, source, identifier,
           {"url": url, "status": status, "observed_at": observed_at.isoformat()},
           identity=request_id, effective=period, known=clock)
