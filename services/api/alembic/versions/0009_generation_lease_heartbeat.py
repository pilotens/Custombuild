"""Add renewable expiry timestamps to generation worker leases.

Revision ID: 0009_generation_lease_heartbeat
Revises: 0008_runtime_reliability
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_generation_lease_heartbeat"
down_revision = "0008_runtime_reliability"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_generation_jobs_status_lease_expires_at"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("generation_jobs")}
    if "lease_expires_at" not in columns:
        op.add_column(
            "generation_jobs",
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "deadline_at" not in columns:
        op.add_column(
            "generation_jobs",
            sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        )

    # A rolling deployment can still have jobs claimed by the previous worker.
    # Give those jobs the former 30-minute safety window instead of expiring
    # them immediately when the new recovery task starts.
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "UPDATE generation_jobs "
                "SET lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '30 minutes' "
                "WHERE status = 'running' AND lease_expires_at IS NULL"
            )
        )
        op.execute(
            sa.text(
                "UPDATE generation_jobs "
                "SET deadline_at = CURRENT_TIMESTAMP + INTERVAL '2 hours' "
                "WHERE status IN ('queued', 'running') AND deadline_at IS NULL"
            )
        )

    indexes = {index["name"] for index in inspector.get_indexes("generation_jobs")}
    if INDEX_NAME not in indexes:
        op.create_index(
            INDEX_NAME,
            "generation_jobs",
            ["status", "lease_expires_at"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "generation_jobs" not in inspector.get_table_names():
        return

    indexes = {index["name"] for index in inspector.get_indexes("generation_jobs")}
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name="generation_jobs")

    columns = {column["name"] for column in inspector.get_columns("generation_jobs")}
    if "deadline_at" in columns:
        op.drop_column("generation_jobs", "deadline_at")
    if "lease_expires_at" in columns:
        op.drop_column("generation_jobs", "lease_expires_at")
