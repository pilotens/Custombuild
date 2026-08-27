"""Immutable tenant evidence for external construction checks.

Revision ID: 0006_external_evidence
Revises: 0005_template_capability_identity
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_external_evidence"
down_revision = "0005_template_capability_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "external_evidence" in inspector.get_table_names():
        return
    op.create_table(
        "external_evidence",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_type", sa.String(length=40), nullable=False),
        sa.Column("rule_id", sa.String(length=40), nullable=False),
        sa.Column("catalog_id", sa.String(length=160), nullable=False),
        sa.Column("catalog_version", sa.String(length=80), nullable=False),
        sa.Column("design_hash", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=160), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_external_evidence_organization_id",
        "external_evidence",
        ["organization_id"],
    )
    op.create_index(
        "ix_external_evidence_project_id", "external_evidence", ["project_id"]
    )
    op.create_index(
        "ix_external_evidence_design_hash", "external_evidence", ["design_hash"]
    )
    op.create_index(
        "ix_external_evidence_project_type",
        "external_evidence",
        ["project_id", "evidence_type"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE external_evidence ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE external_evidence FORCE ROW LEVEL SECURITY")
        op.execute(
            """CREATE POLICY tenant_isolation ON external_evidence
            USING (
                organization_id::text = current_setting('app.current_organization_id', true)
            )
            WITH CHECK (
                organization_id::text = current_setting('app.current_organization_id', true)
            )"""
        )
        op.execute(
            "GRANT SELECT, INSERT ON external_evidence TO custombuild_api, custombuild_worker"
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "external_evidence" in inspector.get_table_names():
        op.drop_table("external_evidence")
