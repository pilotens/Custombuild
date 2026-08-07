"""Persist the exact production engine context on every generation job.

Revision ID: 0002_generation_engine_context
Revises: 0001_initial
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_generation_engine_context"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _column_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {str(column["name"]) for column in inspector.get_columns("generation_jobs")}


def upgrade() -> None:
    # The frozen 0001 baseline deliberately omits this column.  The guard also
    # lets databases made with the pre-freeze development migration converge.
    if "production_engine_context_json" in _column_names():
        return
    op.add_column(
        "generation_jobs",
        sa.Column("production_engine_context_json", sa.JSON(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE generation_jobs SET production_engine_context_json = '{}' "
            "WHERE production_engine_context_json IS NULL"
        )
    )
    op.alter_column("generation_jobs", "production_engine_context_json", nullable=False)


def downgrade() -> None:
    if "production_engine_context_json" in _column_names():
        op.drop_column("generation_jobs", "production_engine_context_json")
