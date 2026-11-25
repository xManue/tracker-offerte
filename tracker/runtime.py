import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

from .catalog import seed_catalog
from .channels import set_source, source_rules
from .demo import seed

HOST = "127.0.0.1"
WORKER_RESTART_SECONDS = 5


def bootstrap(sessions):
    with sessions.begin() as session:
        seed_catalog(session, "real")
        if "local" not in source_rules(session, "real"):
            set_source(session, "real", "local", history=True)
        demo_seeded = seed(session)
        seed_catalog(session, "demo")
        return {"demo_seeded": demo_seeded}


def stop_child(process):
    if process is None or process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_local(port, env_file=None):
    from alembic import command
    from alembic.config import Config
    from dotenv import load_dotenv

    from .db import make_sessions

    if not 1 <= port <= 65535:
        raise ValueError("Porta non valida")
    config = Path.cwd() / "alembic.ini"
    if not config.is_file():
        raise ValueError("Avvia tracker run-local dalla cartella del progetto")
    dotenv_file = Path(".env") if env_file is None else env_file
    if env_file is not None and not dotenv_file.is_file():
        raise ValueError("File .env indicato non trovato")
    if dotenv_file.is_file():
        load_dotenv(dotenv_file, override=False, interpolate=False, encoding="utf-8-sig")

    command.upgrade(Config(str(config)), "head")
    sessions = make_sessions()
    try:
        bootstrap(sessions)
    finally:
        sessions.kw["bind"].dispose()

    web_command = [sys.executable, "-m", "uvicorn", "tracker.web:app",
                   "--host", HOST, "--port", str(port)]
    worker_command = [sys.executable, "-m", "tracker.cli", "worker"]
    print(f"Tracker in avvio su http://{HOST}:{port} con worker locale.", flush=True)
    web = subprocess.Popen(web_command)
    worker = None
    try:
        worker = subprocess.Popen(worker_command)
        while web.poll() is None:
            if worker.poll() is not None:
                print(f"Worker terminato ({worker.returncode}). Riavvio tra {WORKER_RESTART_SECONDS} secondi.",
                      file=sys.stderr, flush=True)
                try:
                    return web.wait(timeout=WORKER_RESTART_SECONDS)
                except subprocess.TimeoutExpired:
                    worker = subprocess.Popen(worker_command)
            time.sleep(0.5)
        return web.returncode
    except KeyboardInterrupt:
        return 0
    finally:
        stop_child(worker)
        stop_child(web)
