import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import DropSchema

from tracker.catalog import catalog
from tracker.db import Base, Event, Job, Reservation, make_sessions
from tracker.restore import restore_sqlite
from tracker.runtime import bootstrap, run_local
from tracker.store import enqueue, record, utc


def test_run_local_loads_env_restarts_worker_and_preserves_existing_archive(tmp_path, monkeypatch):
    project = Path(__file__).resolve().parents[1]
    shutil.copy(project / "alembic.ini", tmp_path)
    shutil.copytree(project / "migrations", tmp_path / "migrations")
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{(tmp_path / 'real.db').as_posix()}"
    monkeypatch.setenv("TRACKER_DATABASE_URL", url)
    monkeypatch.setenv("TRACKER_BRAVE_API_KEY", "restored-after-test")
    monkeypatch.delenv("TRACKER_BRAVE_API_KEY")
    (tmp_path / ".env").write_text(
        "TRACKER_DATABASE_URL=sqlite:///overridden.db\nTRACKER_BRAVE_API_KEY='secret-${example}'\n",
        encoding="utf-8-sig",
    )

    processes = []

    class Child:
        def __init__(self, command, worker):
            self.command, self.worker = command, worker
            self.terminated = False
            self.returncode = 7 if worker and len(processes) == 1 else None

        def poll(self):
            if not self.worker and len(processes) == 3:
                self.returncode = 0
            return self.returncode

        def wait(self, *, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired(self.command, timeout)
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -1

    def start(command):
        assert os.environ["TRACKER_DATABASE_URL"] == url
        assert os.environ["TRACKER_BRAVE_API_KEY"] == "secret-${example}"
        process = Child(command, worker="worker" in command)
        processes.append(process)
        return process

    monkeypatch.setattr("tracker.runtime.subprocess.Popen", start)
    monkeypatch.setattr("tracker.runtime.time.sleep", lambda duration: None)
    assert run_local(8040) == 0
    assert processes[0].command[-4:] == ["--host", "127.0.0.1", "--port", "8040"]
    assert [child.worker for child in processes] == [False, True, True]
    assert processes[-1].terminated
    assert not (tmp_path / "overridden.db").exists()

    sessions = make_sessions(url)
    assert bootstrap(sessions) == {"demo_seeded": False}
    with sessions() as session:
        assert "switch-oled" in catalog(session, "real")
        assert "switch-oled" in catalog(session, "demo")
    sessions.kw["bind"].dispose()


def test_run_local_rejects_an_explicit_missing_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("alembic.ini").touch()
    with pytest.raises(ValueError, match="File .env indicato non trovato"):
        run_local(8000, Path("data/missing.env"))
    assert not Path("data").exists()


def test_restore_sqlite_preserves_ids_in_private_postgres_schema(tmp_path, monkeypatch):
    postgres = os.getenv("TRACKER_TEST_POSTGRES_URL")
    if not postgres:
        pytest.skip("Richiede PostgreSQL di test")

    path = tmp_path / "archivio con spazi.db"
    source = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(source)
    with sessionmaker(source).begin() as session:
        first = record(session, "real", "offer", "local", "one", {"title": "Offerta"})
        job = enqueue(session, "real", "evaluate", "first", {"event_id": first})
        session.execute(Reservation.__table__.insert(), {"id": 8, "mode": "real", "unit": "local:one", "plan": "p"})
        observed = session.execute(Event.__table__.select()).mappings().one()
    source.dispose()

    schema = "tracker_restore_" + uuid4().hex
    monkeypatch.setenv("TRACKER_DATABASE_URL", postgres)
    monkeypatch.setenv("TRACKER_DATABASE_SCHEMA", schema)
    admin = create_engine(postgres)
    sessions = make_sessions()
    try:
        command.upgrade(Config("alembic.ini"), "head")
        with sessions.begin() as session:
            assert session.scalar(text("SELECT current_schema()")) == schema
            assert restore_sqlite(session, path) == {"events": 1, "jobs": 1, "reservations": 1}
            assert session.get(Job, job).data == {"event_id": first}
            assert session.get(Reservation, 8).unit == "local:one"
            assert session.get(Event, first).known_at == utc(observed["known_at"])
        with pytest.raises(ValueError, match="database del tracker vuoto"), sessions.begin() as session:
            restore_sqlite(session, path)
        with sessions.begin() as session:
            assert record(session, "real", "offer", "local", "two", {}) == first + 1
            assert enqueue(session, "real", "evaluate", "second", {}) == job + 1
    finally:
        sessions.kw["bind"].dispose()
        with admin.begin() as conn:
            conn.execute(DropSchema(schema, if_exists=True, cascade=True))
        admin.dispose()
