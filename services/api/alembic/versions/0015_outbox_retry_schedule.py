"""Add durable availability boundaries for job and outbox retries.

Revision ID: 0015_outbox_retry_schedule
Revises: 0014_release_generation_binding
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_outbox_retry_schedule"
down_revision = "0014_release_generation_binding"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_outbox_events_pending_available"
JOB_INDEX_NAME = "ix_generation_jobs_status_next_attempt_at"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    outbox_columns = {column["name"] for column in inspector.get_columns("outbox_events")}
    if "available_at" not in outbox_columns:
        op.add_column(
            "outbox_events",
            sa.Column(
                "available_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )

    outbox_indexes = {index["name"] for index in inspector.get_indexes("outbox_events")}
    if INDEX_NAME not in outbox_indexes:
        op.create_index(
            INDEX_NAME,
            "outbox_events",
            ["organization_id", "available_at", "created_at", "id"],
            postgresql_where=sa.text("dispatched_at IS NULL AND dead_lettered_at IS NULL"),
        )

    job_columns = {column["name"] for column in inspector.get_columns("generation_jobs")}
    if "next_attempt_at" not in job_columns:
        op.add_column(
            "generation_jobs",
            sa.Column(
                "next_attempt_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
        bind.execute(
            sa.text(
                "UPDATE generation_jobs SET next_attempt_at = CURRENT_TIMESTAMP "
                "WHERE status = 'queued'"
            )
        )
        if bind.dialect.name != "sqlite":
            op.alter_column(
                "generation_jobs",
                "next_attempt_at",
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )

    job_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("generation_jobs")}
    if JOB_INDEX_NAME not in job_indexes:
        op.create_index(
            JOB_INDEX_NAME,
            "generation_jobs",
            ["status", "next_attempt_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "outbox_events" not in inspector.get_table_names():
        return

    indexes = {index["name"] for index in inspector.get_indexes("outbox_events")}
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name="outbox_events")

    columns = {column["name"] for column in inspector.get_columns("outbox_events")}
    if "available_at" in columns:
        op.drop_column("outbox_events", "available_at")

    job_indexes = {index["name"] for index in inspector.get_indexes("generation_jobs")}
    if JOB_INDEX_NAME in job_indexes:
        op.drop_index(JOB_INDEX_NAME, table_name="generation_jobs")
    job_columns = {column["name"] for column in inspector.get_columns("generation_jobs")}
    if "next_attempt_at" in job_columns:
        op.drop_column("generation_jobs", "next_attempt_at")
