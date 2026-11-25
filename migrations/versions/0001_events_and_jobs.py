from alembic import op
import sqlalchemy as sa

revision = "0001_events_and_jobs"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(240), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.UniqueConstraint("mode", "kind", "source", "external_id", "fingerprint"),
    )
    op.create_index("ix_events_stream", "events", ["mode", "kind", "source", "external_id", "id"])
    op.create_index("ix_events_history", "events", ["mode", "kind", "known_at", "id"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("key", sa.String(240), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(32)),
        sa.Column("error", sa.String(240)),
        sa.UniqueConstraint("mode", "key"),
    )
    op.create_index("ix_jobs_ready", "jobs", ["mode", "status", "run_at", "lease_until"])
    op.create_table(
        "reservations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("unit", sa.String(240), nullable=False),
        sa.Column("plan", sa.String(32), nullable=False),
        sa.UniqueConstraint("mode", "unit"),
    )


def downgrade():
    op.drop_table("reservations")
    op.drop_table("jobs")
    op.drop_table("events")
