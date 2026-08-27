"""Optimistic drafts, worker leases and bounded outbox delivery.

Revision ID: 0008_runtime_reliability
Revises: 0007_imported_reference_assets
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_runtime_reliability"
down_revision = "0007_imported_reference_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    project_columns = {column["name"] for column in inspector.get_columns("projects")}
    if "draft_revision" not in project_columns:
        op.add_column(
            "projects",
            sa.Column(
                "draft_revision",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    job_columns = {
        column["name"] for column in inspector.get_columns("generation_jobs")
    }
    if "lease_token" not in job_columns:
        op.add_column(
            "generation_jobs",
            sa.Column("lease_token", sa.String(length=36), nullable=True),
        )

    outbox_columns = {
        column["name"] for column in inspector.get_columns("outbox_events")
    }
    if "dead_lettered_at" not in outbox_columns:
        op.add_column(
            "outbox_events",
            sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_outbox_events_dead_lettered_at",
            "outbox_events",
            ["dead_lettered_at"],
        )
    if "last_error" not in outbox_columns:
        op.add_column(
            "outbox_events",
            sa.Column("last_error", sa.String(length=500), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    if "outbox_events" in inspector.get_table_names():
        outbox_columns = {
            column["name"] for column in inspector.get_columns("outbox_events")
        }
        if "last_error" in outbox_columns:
            op.drop_column("outbox_events", "last_error")
        if "dead_lettered_at" in outbox_columns:
            op.drop_index(
                "ix_outbox_events_dead_lettered_at",
                table_name="outbox_events",
            )
            op.drop_column("outbox_events", "dead_lettered_at")

    if "generation_jobs" in inspector.get_table_names():
        job_columns = {
            column["name"] for column in inspector.get_columns("generation_jobs")
        }
        if "lease_token" in job_columns:
            op.drop_column("generation_jobs", "lease_token")

    if "projects" in inspector.get_table_names():
        project_columns = {
            column["name"] for column in inspector.get_columns("projects")
        }
        if "draft_revision" in project_columns:
            op.drop_column("projects", "draft_revision")
