import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import JSON, DateTime, Index, Integer, String, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now():
    return datetime.now(UTC)


def database_url():
    if value := os.getenv("TRACKER_DATABASE_URL"):
        return value
    path = Path.cwd() / "data"
    path.mkdir(exist_ok=True)
    return f"sqlite:///{(path / 'tracker.db').as_posix()}"


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("mode", "kind", "source", "external_id", "fingerprint"),
        Index("ix_events_stream", "mode", "kind", "source", "external_id", "id"),
        Index("ix_events_history", "mode", "kind", "known_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(8))
    kind: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(240))
    fingerprint: Mapped[str] = mapped_column(String(64))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    data: Mapped[dict] = mapped_column(JSON)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("mode", "key"),
        Index("ix_jobs_ready", "mode", "status", "run_at", "lease_until"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(8))
    key: Mapped[str] = mapped_column(String(240))
    kind: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(String(240))


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (UniqueConstraint("mode", "unit"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(8))
    unit: Mapped[str] = mapped_column(String(240))
    plan: Mapped[str] = mapped_column(String(32))


def make_engine(url=None):
    return create_engine(url or database_url(), pool_pre_ping=True)


def make_sessions(url=None):
    return sessionmaker(make_engine(url), expire_on_commit=False)
