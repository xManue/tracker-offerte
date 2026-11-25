import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tracker.catalog import catalog
from tracker.db import make_sessions
from tracker.runtime import bootstrap, run_local


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
