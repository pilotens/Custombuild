"""Create the frozen initial tenant-aware schema and row-level security.

Revision ID: 0001_initial
Revises: None
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


CATALOG_TABLES = (
    "furniture_templates",
    "hardware_items",
    "joint_definitions",
    "machine_profiles",
    "material_property_versions",
    "materials",
    "postprocessor_versions",
    "stock_items",
    "template_versions",
    "tool_definitions",
)

PRODUCTION_TABLES = (
    "assembly_graphs",
    "assembly_steps",
    "bom_lines",
    "cam_operations",
    "constraints",
    "cut_list_lines",
    "load_cases",
    "manufacturing_features",
    "nesting_layouts",
    "parameter_definitions",
    "parameter_values",
    "part_faces",
    "parts",
    "rule_evaluations",
    "setups",
    "toolpaths",
)

TENANT_TABLES = (
    "audit_events",
    "furniture_templates",
    "hardware_items",
    "joint_definitions",
    "machine_profiles",
    "material_property_versions",
    "materials",
    "memberships",
    "outbox_events",
    "postprocessor_versions",
    "projects",
    "stock_items",
    "template_versions",
    "tool_definitions",
    "design_versions",
    "designs",
    "assembly_graphs",
    "assembly_steps",
    "bom_lines",
    "cam_operations",
    "constraints",
    "cut_list_lines",
    "generation_jobs",
    "load_cases",
    "manufacturing_features",
    "nesting_layouts",
    "parameter_definitions",
    "parameter_values",
    "part_faces",
    "parts",
    "releases",
    "rule_evaluations",
    "setups",
    "toolpaths",
    "approvals",
    "artifacts",
)

CREATION_ORDER = (
    "organizations",
    "users",
    "audit_events",
    *CATALOG_TABLES,
    "memberships",
    "outbox_events",
    "projects",
    "design_versions",
    "designs",
    *PRODUCTION_TABLES,
    "generation_jobs",
    "releases",
    "approvals",
    "artifacts",
)


def _id_column() -> sa.Column[str]:
    return sa.Column("id", sa.String(length=36), nullable=False)


def _timestamp_columns() -> tuple[sa.Column[Any], sa.Column[Any]]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def _tenant_column() -> sa.Column[str]:
    return sa.Column("organization_id", sa.String(length=36), nullable=False)


def _tenant_foreign_key() -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"], ["organizations.id"], ondelete="CASCADE"
    )


def _create_tenant_index(table_name: str) -> None:
    op.create_index(
        op.f(f"ix_{table_name}_organization_id"),
        table_name,
        ["organization_id"],
        unique=False,
    )


def _create_catalog_table(table_name: str) -> None:
    op.create_table(
        table_name,
        sa.Column("name", sa.String(length=180), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        _id_column(),
        *_timestamp_columns(),
        _tenant_column(),
        _tenant_foreign_key(),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_tenant_index(table_name)


def _create_production_table(table_name: str) -> None:
    op.create_table(
        table_name,
        sa.Column("design_version_id", sa.String(length=36), nullable=False),
        sa.Column("stable_key", sa.String(length=160), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=False),
        _id_column(),
        *_timestamp_columns(),
        _tenant_column(),
        sa.ForeignKeyConstraint(
            ["design_version_id"], ["design_versions.id"], ondelete="CASCADE"
        ),
        _tenant_foreign_key(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f(f"ix_{table_name}_design_version_id"),
        table_name,
        ["design_version_id"],
        unique=False,
    )
    _create_tenant_index(table_name)


def _create_core_tables() -> None:
    op.create_table(
        "organizations",
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        _id_column(),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_organizations_slug"), "organizations", ["slug"], unique=True)

    op.create_table(
        "users",
        sa.Column("oidc_sub", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        _id_column(),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index(op.f("ix_users_oidc_sub"), "users", ["oidc_sub"], unique=True)

    op.create_table(
        "audit_events",
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", sa.String(length=80), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        _id_column(),
        _tenant_column(),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        _tenant_foreign_key(),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_tenant_index("audit_events")
    op.create_index(
        "ix_audit_org_time",
        "audit_events",
        ["organization_id", "occurred_at"],
        unique=False,
    )

    for table_name in CATALOG_TABLES:
        _create_catalog_table(table_name)

    op.create_table(
        "memberships",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner",
                "admin",
                "designer",
                "reviewer",
                "production",
                "operator",
                "viewer",
                name="role",
                native_enum=False,
            ),
            nullable=False,
        ),
        _id_column(),
        *_timestamp_columns(),
        _tenant_column(),
        _tenant_foreign_key(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "user_id"),
    )
    _create_tenant_index("memberships")

    op.create_table(
        "outbox_events",
        sa.Column("event_key", sa.String(length=100), nullable=False),
        sa.Column("topic", sa.String(length=100), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        _id_column(),
        *_timestamp_columns(),
        _tenant_column(),
        _tenant_foreign_key(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key"),
    )
    _create_tenant_index("outbox_events")

    op.create_table(
        "projects",
        sa.Column("name", sa.String(length=180), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("furniture_type", sa.String(length=80), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        _id_column(),
        *_timestamp_columns(),
        _tenant_column(),
        _tenant_foreign_key(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "name"),
    )
    _create_tenant_index("projects")

    op.create_table(
        "design_versions",
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "concept",
                "draft",
                "design_validated",
                "cam_validated",
                "approved",
                "released",
                "superseded",
                "archived",
                name="designstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("design_hash", sa.String(length=64), nullable=False),
        sa.Column("context_hash", sa.String(length=64), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("engine_version", sa.String(length=40), nullable=False),
        sa.Column("template_version", sa.String(length=40), nullable=False),
        sa.Column("rule_version", sa.String(length=40), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("immutable", sa.Boolean(), nullable=False),
        _id_column(),
        *_timestamp_columns(),
        _tenant_column(),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        _tenant_foreign_key(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "revision"),
    )
    op.create_index(
        op.f("ix_design_versions_context_hash"),
        "design_versions",
        ["context_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_design_versions_design_hash"),
        "design_versions",
        ["design_hash"],
        unique=False,
    )
    _create_tenant_index("design_versions")
    op.create_index(
        op.f("ix_design_versions_project_id"),
        "design_versions",
        ["project_id"],
        unique=False,
    )

    op.create_table(
        "designs",
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=180), nullable=False),
        _id_column(),
        *_timestamp_columns(),
        _tenant_column(),
        _tenant_foreign_key(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_tenant_index("designs")


def _create_generation_tables() -> None:
    for table_name in PRODUCTION_TABLES:
        _create_production_table(table_name)

    op.create_table(
        "generation_jobs",
        sa.Column("design_version_id", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                name="jobstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("production_context_hash", sa.String(length=64), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _id_column(),
        *_timestamp_columns(),
        _tenant_column(),
        sa.ForeignKeyConstraint(
            ["design_version_id"], ["design_versions.id"], ondelete="CASCADE"
        ),
        _tenant_foreign_key(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "idempotency_key"),
    )
    op.create_index(
        op.f("ix_generation_jobs_design_version_id"),
        "generation_jobs",
        ["design_version_id"],
        unique=False,
    )
    _create_tenant_index("generation_jobs")
    op.create_index(
        op.f("ix_generation_jobs_status"),
        "generation_jobs",
        ["status"],
        unique=False,
    )

    op.create_table(
        "releases",
        sa.Column("design_version_id", sa.String(length=36), nullable=False),
        sa.Column("release_number", sa.String(length=80), nullable=False),
        sa.Column("released_by", sa.String(length=36), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        _id_column(),
        *_timestamp_columns(),
        _tenant_column(),
        sa.ForeignKeyConstraint(
            ["design_version_id"], ["design_versions.id"], ondelete="CASCADE"
        ),
        _tenant_foreign_key(),
        sa.ForeignKeyConstraint(["released_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("design_version_id"),
    )
    _create_tenant_index("releases")

    op.create_table(
        "approvals",
        sa.Column("design_version_id", sa.String(length=36), nullable=False),
        sa.Column("approval_type", sa.String(length=80), nullable=False),
        sa.Column("approved_by", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("generation_job_id", sa.String(length=36), nullable=True),
        sa.Column("production_context_hash", sa.String(length=64), nullable=True),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("overrides_json", sa.JSON(), nullable=False),
        _id_column(),
        *_timestamp_columns(),
        _tenant_column(),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["design_version_id"], ["design_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["generation_job_id"], ["generation_jobs.id"], ondelete="CASCADE"
        ),
        _tenant_foreign_key(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("design_version_id", "approval_type"),
    )
    _create_tenant_index("approvals")

    op.create_table(
        "artifacts",
        sa.Column("generation_job_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=80), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=160), nullable=False),
        _id_column(),
        *_timestamp_columns(),
        _tenant_column(),
        sa.ForeignKeyConstraint(
            ["generation_job_id"], ["generation_jobs.id"], ondelete="CASCADE"
        ),
        _tenant_foreign_key(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_job_id", "kind"),
    )
    op.create_index(
        op.f("ix_artifacts_generation_job_id"),
        "artifacts",
        ["generation_job_id"],
        unique=False,
    )
    _create_tenant_index("artifacts")


def _enable_row_level_security() -> None:
    for table_name in TENANT_TABLES:
        quoted_table = op.get_context().dialect.identifier_preparer.quote(table_name)
        op.execute(f"ALTER TABLE {quoted_table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {quoted_table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""CREATE POLICY tenant_isolation ON {quoted_table}
            USING (
                organization_id::text = current_setting('app.current_organization_id', true)
            )
            WITH CHECK (
                organization_id::text = current_setting('app.current_organization_id', true)
            )"""
        )


def upgrade() -> None:
    _create_core_tables()
    _create_generation_tables()
    _enable_row_level_security()
    op.execute("GRANT USAGE ON SCHEMA public TO custombuild_api, custombuild_worker")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
        "TO custombuild_api, custombuild_worker"
    )
    op.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
        "TO custombuild_api, custombuild_worker"
    )


def downgrade() -> None:
    for table_name in reversed(CREATION_ORDER):
        op.drop_table(table_name)
