"""Persist audited source provenance on frozen design revisions.

Revision ID: 0004_design_source_provenance
Revises: 0003_project_workspace_draft
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_design_source_provenance"
down_revision = "0003_project_workspace_draft"
branch_labels = None
depends_on = None


def _column_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {str(column["name"]) for column in inspector.get_columns("design_versions")}


def upgrade() -> None:
    if "source_provenance_json" in _column_names():
        return
    op.add_column(
        "design_versions",
        sa.Column("source_provenance_json", sa.JSON(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE design_versions SET source_provenance_json = '{}' "
            "WHERE source_provenance_json IS NULL"
        )
    )
    op.alter_column("design_versions", "source_provenance_json", nullable=False)


def downgrade() -> None:
    if "source_provenance_json" in _column_names():
        op.drop_column("design_versions", "source_provenance_json")
