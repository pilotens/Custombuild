"""Immutable, project-bound source images for reference imports.

Revision ID: 0007_imported_reference_assets
Revises: 0006_external_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_imported_reference_assets"
down_revision = "0006_external_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "imported_assets" not in tables:
        op.create_table(
            "imported_assets",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("organization_id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("object_key", sa.String(length=512), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("media_type", sa.String(length=160), nullable=False),
            sa.Column("original_filename", sa.String(length=255), nullable=False),
            sa.Column("created_by", sa.String(length=36), nullable=False),
            sa.ForeignKeyConstraint(
                ["organization_id"], ["organizations.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "organization_id",
                "project_id",
                "sha256",
                name="uq_imported_assets_project_digest",
            ),
            sa.UniqueConstraint(
                "organization_id",
                "project_id",
                "id",
                name="uq_imported_assets_tenant_project_id",
            ),
        )
        op.create_index(
            "ix_imported_assets_organization_id",
            "imported_assets",
            ["organization_id"],
        )
        op.create_index("ix_imported_assets_project_id", "imported_assets", ["project_id"])
        op.create_index("ix_imported_assets_sha256", "imported_assets", ["sha256"])
        op.create_index(
            "ix_imported_assets_project_created",
            "imported_assets",
            ["project_id", "created_at"],
        )

    design_columns = {column["name"] for column in inspector.get_columns("design_versions")}
    if "source_import_id" not in design_columns:
        op.add_column(
            "design_versions",
            sa.Column("source_import_id", sa.String(length=36), nullable=True),
        )
        op.create_foreign_key(
            "fk_design_versions_source_import_id",
            "design_versions",
            "imported_assets",
            ["organization_id", "project_id", "source_import_id"],
            ["organization_id", "project_id", "id"],
            ondelete="RESTRICT",
        )
        op.create_index(
            "ix_design_versions_source_import_id",
            "design_versions",
            ["source_import_id"],
        )

    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE imported_assets ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE imported_assets FORCE ROW LEVEL SECURITY")
        op.execute(
            """CREATE POLICY tenant_isolation ON imported_assets
            USING (
                organization_id::text = current_setting('app.current_organization_id', true)
            )
            WITH CHECK (
                organization_id::text = current_setting('app.current_organization_id', true)
            )"""
        )
        op.execute(
            "GRANT SELECT, INSERT ON imported_assets TO custombuild_api, custombuild_worker"
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "design_versions" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("design_versions")}
        if "source_import_id" in columns:
            op.drop_index("ix_design_versions_source_import_id", table_name="design_versions")
            op.drop_constraint(
                "fk_design_versions_source_import_id",
                "design_versions",
                type_="foreignkey",
            )
            op.drop_column("design_versions", "source_import_id")
    if "imported_assets" in inspector.get_table_names():
        op.drop_table("imported_assets")
