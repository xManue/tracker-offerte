from sqlalchemy import DateTime, create_engine, select, text
from sqlalchemy.engine import URL

from .db import Event, Job, Reservation
from .store import utc


def restore_sqlite(session, path):
    if session.bind.dialect.name != "postgresql":
        raise ValueError("Il ripristino richiede una destinazione PostgreSQL")
    if not path.is_file():
        raise ValueError("Database SQLite di origine non trovato")

    tables = (Event.__table__, Job.__table__, Reservation.__table__)
    session.execute(text("LOCK TABLE events, jobs, reservations IN ACCESS EXCLUSIVE MODE"))
    for table in tables:
        if session.scalar(select(table.c.id).limit(1)) is not None:
            raise ValueError("Ripristina lo storico in un database del tracker vuoto")

    url = URL.create("sqlite", database=path.resolve().as_uri(), query={"mode": "ro", "uri": "true"})
    engine = create_engine(url)
    counts, last_ids = {}, {}
    try:
        with engine.connect() as source:
            source.exec_driver_sql("BEGIN")
            for table in tables:
                dates = [column.name for column in table.columns if isinstance(column.type, DateTime)]
                counts[table.name] = 0
                records = source.execute(select(table).order_by(table.c.id)).mappings()
                for batch in records.partitions(500):
                    rows = [dict(row) for row in batch]
                    for row in rows:
                        for field in dates:
                            if row[field] is not None:
                                row[field] = utc(row[field])
                    session.execute(table.insert(), rows)
                    counts[table.name] += len(rows)
                    last_ids[table.name] = rows[-1]["id"]
    finally:
        engine.dispose()
    for name, last_id in last_ids.items():
        session.execute(text("SELECT setval(pg_get_serial_sequence(:name, 'id'), :last_id)"),
                        {"name": name, "last_id": last_id})
    return counts
