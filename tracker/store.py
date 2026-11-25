import hashlib
import json
from datetime import UTC, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import Event, Job, now


def utc(value):
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def canonical(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def insert_once(session, model, fields, values):
    dialect = session.bind.dialect.name
    insert = {"postgresql": postgres_insert, "sqlite": sqlite_insert}[dialect]
    statement = insert(model).values(**values).on_conflict_do_nothing(index_elements=fields)
    return session.execute(statement.returning(model.id)).scalar_one_or_none()


def record(session, mode, kind, source, external_id, data, *, effective=None, known=None, identity=None):
    timestamp = now()
    return insert_once(
        session, Event, ["mode", "kind", "source", "external_id", "fingerprint"],
        dict(mode=mode, kind=kind, source=source, external_id=str(external_id),
             data=data, fingerprint=digest(canonical(data) if identity is None else str(identity)),
             effective_at=utc(effective or timestamp), known_at=utc(known or timestamp),
             ingested_at=timestamp),
    )


def events(session, mode, kind, *, cutoff=None, source=None, external_id=None):
    query = select(Event).where(Event.mode == mode, Event.kind == kind)
    if cutoff is not None:
        query = query.where(Event.known_at <= cutoff, Event.effective_at <= cutoff)
    if source is not None:
        query = query.where(Event.source == source)
    if external_id is not None:
        query = query.where(Event.external_id == external_id)
    return list(session.scalars(query.order_by(Event.effective_at, Event.known_at, Event.id)))


def latest(session, mode, kind, *, cutoff=None, where=None):
    query = select(
        Event.id, func.row_number().over(
            partition_by=(Event.source, Event.external_id),
            order_by=(Event.effective_at.desc(), Event.known_at.desc(), Event.id.desc()),
        ).label("position"),
    ).where(Event.mode == mode, Event.kind == kind)
    if cutoff is not None:
        query = query.where(Event.known_at <= cutoff, Event.effective_at <= cutoff)
    if where is not None:
        query = query.where(where)
    ranked = query.subquery()
    return list(session.scalars(
        select(Event).join(ranked, ranked.c.id == Event.id).where(ranked.c.position == 1)
        .order_by(Event.source, Event.external_id)
    ))


def enqueue(session, mode, kind, key, data, run_at=None):
    key = key if len(key) <= 240 else f"{kind}:{digest(key)}"
    return insert_once(
        session, Job, ["mode", "key"],
        dict(mode=mode, kind=kind, key=key, data=data, status="pending", attempts=0, run_at=run_at or now()),
    )


def schedule_offer(session, mode, source, external_id, event_id, *, refresh=False):
    enqueue(session, mode, "evaluate", f"evaluate:offer:{event_id}", {"source": source, "id": external_id})
    if refresh:
        enqueue(session, mode, "refresh", f"refresh:{event_id}",
                {"source": source, "id": external_id}, now() + timedelta(hours=3))
