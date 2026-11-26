import os
import sys
import time
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, OperationalError

from .codex_index import synchronize
from .db import database_url, make_sessions


POLL_SECONDS = 15
LEASE_SECONDS = 90
EXCHANGE = Path("data/codex-cloud")


def bridge_url(env_file):
    if not env_file.is_file():
        raise ValueError("File di configurazione cloud assente")
    config = dotenv_values(env_file, encoding="utf-8-sig", interpolate=False)
    if not config.get("TRACKER_DATABASE_URL"):
        raise ValueError("Imposta TRACKER_DATABASE_URL nella configurazione cloud")
    os.environ.update({key: value for key, value in config.items() if value is not None})
    os.environ["TRACKER_DATABASE_SCHEMA"] = config.get("TRACKER_DATABASE_SCHEMA") or "tracker"
    os.environ["TRACKER_CODEX_DISCOVERY"] = "1"
    try:
        url = make_url(database_url())
    except ArgumentError:
        raise ValueError("Stringa di connessione PostgreSQL non valida") from None
    if url.drivername != "postgresql+psycopg":
        raise ValueError("Il collegamento Codex richiede PostgreSQL con psycopg")
    if not url.password or url.password in {"PASSWORD", "[YOUR-DATABASE-PASSWORD]", "YOUR-DATABASE-PASSWORD"}:
        raise ValueError("Inserisci la password PostgreSQL nella configurazione cloud")
    if url.host and url.host.endswith((".supabase.com", ".supabase.co")):
        if url.port == 6543:
            raise ValueError("Usa la connessione diretta o il Session pooler Supabase sulla porta 5432")
        certificate = url.query.get("sslrootcert")
        if certificate is None or certificate == "/app/data/supabase-ca.crt":
            certificate = env_file.resolve().parent / "data" / "supabase-ca.crt"
        if not Path(certificate).is_file():
            raise ValueError("Certificato TLS Supabase assente")
        url = url.update_query_dict({"sslmode": "verify-full", "sslrootcert": str(certificate), "connect_timeout": "10"})
    return url


def run_codex_bridge(env_file, *, once=False):
    sessions = make_sessions(bridge_url(env_file))
    try:
        while True:
            try:
                synchronize(sessions, EXCHANGE, lease_seconds=LEASE_SECONDS)
            except OperationalError:
                if once:
                    raise ValueError("Connessione al database cloud non riuscita") from None
                print("Connessione al database cloud non riuscita. Nuovo tentativo tra 15 secondi.",
                      file=sys.stderr, flush=True)
            if once:
                return
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        return
    finally:
        sessions.kw["bind"].dispose()
