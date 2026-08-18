"""Bind frozen revisions to the server template-capability registry.

Revision ID: 0005_template_capability_identity
Revises: 0004_design_source_provenance
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_template_capability_identity"
down_revision = "0004_design_source_provenance"
branch_labels = None
depends_on = None


def _column_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {str(column["name"]) for column in inspector.get_columns("design_versions")}


def upgrade() -> None:
    # Alembic creates version_num as VARCHAR(32), while this repository uses
    # descriptive revision IDs. Widen it before Alembic records this revision;
    # otherwise a fresh PostgreSQL upgrade rolls back after the DDL succeeds.
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(length=32),
            type_=sa.String(length=128),
            existing_nullable=False,
        )
    columns = _column_names()
    if "template_id" not in columns:
        op.add_column(
            "design_versions",
            sa.Column(
                "template_id",
                sa.String(length=80),
                nullable=False,
                server_default="shelving",
            ),
        )
    if "template_capability_fingerprint" not in columns:
        # Existing revisions are intentionally marked legacy.  Generation rejects
        # this sentinel and requires a new server-screened revision.
        op.add_column(
            "design_versions",
            sa.Column(
                "template_capability_fingerprint",
                sa.String(length=64),
                nullable=False,
                server_default="0" * 64,
            ),
        )


def downgrade() -> None:
    columns = _column_names()
    if "template_capability_fingerprint" in columns:
        op.drop_column("design_versions", "template_capability_fingerprint")
    if "template_id" in columns:
        op.drop_column("design_versions", "template_id")
