"""Bind every parent-child edge to the same tenant.

Revision ID: 0010_tenant_graph_foreign_keys
Revises: 0009_generation_lease_heartbeat
"""

from dataclasses import dataclass

import sqlalchemy as sa
from alembic import op

revision = "0010_tenant_graph_foreign_keys"
down_revision = "0009_generation_lease_heartbeat"
branch_labels = None
depends_on = None


@dataclass(frozen=True)
class TenantRelationship:
    child_table: str
    child_column: str
    parent_table: str
    old_constraint: str
    tenant_constraint: str


PROJECT_RELATIONSHIPS = (
    TenantRelationship(
        "design_versions",
        "project_id",
        "projects",
        "design_versions_project_id_fkey",
        "fk_design_versions_org_project",
    ),
    TenantRelationship(
        "designs",
        "project_id",
        "projects",
        "designs_project_id_fkey",
        "fk_designs_org_project",
    ),
    TenantRelationship(
        "imported_assets",
        "project_id",
        "projects",
        "imported_assets_project_id_fkey",
        "fk_imported_assets_org_project",
    ),
    TenantRelationship(
        "external_evidence",
        "project_id",
        "projects",
        "external_evidence_project_id_fkey",
        "fk_external_evidence_org_project",
    ),
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

DESIGN_VERSION_RELATIONSHIPS = (
    TenantRelationship(
        "generation_jobs",
        "design_version_id",
        "design_versions",
        "generation_jobs_design_version_id_fkey",
        "fk_generation_jobs_org_design_version",
    ),
    TenantRelationship(
        "releases",
        "design_version_id",
        "design_versions",
        "releases_design_version_id_fkey",
        "fk_releases_org_design_version",
    ),
    TenantRelationship(
        "approvals",
        "design_version_id",
        "design_versions",
        "approvals_design_version_id_fkey",
        "fk_approvals_org_design_version",
    ),
    *(
        TenantRelationship(
            table_name,
            "design_version_id",
            "design_versions",
            f"{table_name}_design_version_id_fkey",
            f"fk_{table_name}_org_design_version",
        )
        for table_name in PRODUCTION_TABLES
    ),
)

GENERATION_JOB_RELATIONSHIPS = (
    TenantRelationship(
        "artifacts",
        "generation_job_id",
        "generation_jobs",
        "artifacts_generation_job_id_fkey",
        "fk_artifacts_org_generation_job",
    ),
    TenantRelationship(
        "approvals",
        "generation_job_id",
        "generation_jobs",
        "approvals_generation_job_id_fkey",
        "fk_approvals_org_generation_job",
    ),
)

TENANT_RELATIONSHIPS = (
    *PROJECT_RELATIONSHIPS,
    *DESIGN_VERSION_RELATIONSHIPS,
    *GENERATION_JOB_RELATIONSHIPS,
)

PARENT_UNIQUES = (
    ("projects", "uq_projects_org_id"),
    ("design_versions", "uq_design_versions_org_id"),
    ("generation_jobs", "uq_generation_jobs_org_id"),
)


def _preflight_existing_rows() -> None:
    """Abort before DDL when an existing child contradicts its parent's tenant."""

    bind = op.get_bind()
    organization_ids = tuple(
        str(value)
        for value in bind.execute(
            sa.text("SELECT id FROM organizations ORDER BY id")
        ).scalars()
    )
    conflict_counts = {relationship: 0 for relationship in TENANT_RELATIONSHIPS}
    try:
        for organization_id in organization_ids:
            # FORCE RLS also applies to a NOBYPASS table owner.  Enumerate the
            # non-RLS organization table and expose exactly one tenant at a time.
            bind.execute(
                sa.text(
                    "SELECT set_config("
                    "'app.current_organization_id', :organization_id, true)"
                ),
                {"organization_id": organization_id},
            )
            for relationship in TENANT_RELATIONSHIPS:
                # Identifiers only come from the frozen constants above. Values
                # are parameterized. Under the child's tenant context, a parent
                # from another tenant is hidden and therefore becomes NULL.
                mismatch_count = bind.execute(
                    sa.text(
                        f"SELECT count(*) FROM {relationship.child_table} AS child "  # noqa: S608
                        f"LEFT JOIN {relationship.parent_table} AS parent "
                        f"ON parent.id = child.{relationship.child_column} "
                        f"WHERE child.{relationship.child_column} IS NOT NULL "
                        "AND (parent.id IS NULL "
                        "OR child.organization_id <> parent.organization_id)"
                    )
                ).scalar_one()
                conflict_counts[relationship] += int(mismatch_count)
    finally:
        # An empty value matches no valid organization id and prevents a later
        # statement in the Alembic transaction from inheriting the final tenant.
        bind.execute(
            sa.text("SELECT set_config('app.current_organization_id', '', true)")
        )

    conflicts = [
        (
            f"{relationship.child_table}.{relationship.child_column} -> "
            f"{relationship.parent_table}.id has {mismatch_count} conflicting row(s)"
        )
        for relationship, mismatch_count in conflict_counts.items()
        if mismatch_count
    ]
    if conflicts:
        raise RuntimeError("TENANT_GRAPH_PRECHECK_FAILED: " + "; ".join(conflicts))


def _replace_with_tenant_foreign_key(relationship: TenantRelationship) -> None:
    op.drop_constraint(
        relationship.old_constraint,
        relationship.child_table,
        type_="foreignkey",
    )
    op.create_foreign_key(
        relationship.tenant_constraint,
        relationship.child_table,
        relationship.parent_table,
        ["organization_id", relationship.child_column],
        ["organization_id", "id"],
        ondelete="CASCADE",
    )


def _restore_single_column_foreign_key(relationship: TenantRelationship) -> None:
    op.drop_constraint(
        relationship.tenant_constraint,
        relationship.child_table,
        type_="foreignkey",
    )
    op.create_foreign_key(
        relationship.old_constraint,
        relationship.child_table,
        relationship.parent_table,
        [relationship.child_column],
        ["id"],
        ondelete="CASCADE",
    )


def upgrade() -> None:
    _preflight_existing_rows()

    for table_name, constraint_name in PARENT_UNIQUES:
        op.create_unique_constraint(
            constraint_name,
            table_name,
            ["organization_id", "id"],
        )

    for relationship in TENANT_RELATIONSHIPS:
        _replace_with_tenant_foreign_key(relationship)


def downgrade() -> None:
    for relationship in reversed(TENANT_RELATIONSHIPS):
        _restore_single_column_foreign_key(relationship)

    for table_name, constraint_name in reversed(PARENT_UNIQUES):
        op.drop_constraint(constraint_name, table_name, type_="unique")
