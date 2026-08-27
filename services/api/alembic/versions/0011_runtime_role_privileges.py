"""Replace blanket runtime grants with explicit least-privilege access.

Revision ID: 0011_runtime_role_privileges
Revises: 0010_tenant_graph_foreign_keys
"""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op

revision = "0011_runtime_role_privileges"
down_revision = "0010_tenant_graph_foreign_keys"
branch_labels = None
depends_on = None


RUNTIME_ROLES = ("custombuild_api", "custombuild_worker")

# The API owns the synchronous user workflow. Tables omitted from this mapping
# are intentionally inaccessible even when Row-Level Security would otherwise
# hide cross-tenant rows.
API_TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "users": ("SELECT",),
    "memberships": ("SELECT",),
    "projects": ("SELECT", "INSERT", "UPDATE"),
    "design_versions": ("SELECT", "INSERT", "UPDATE"),
    "imported_assets": ("SELECT", "INSERT"),
    "external_evidence": ("SELECT", "INSERT"),
    "generation_jobs": ("SELECT", "INSERT", "UPDATE"),
    "outbox_events": ("INSERT",),
    "approvals": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "releases": ("SELECT", "INSERT"),
    "artifacts": ("SELECT", "DELETE"),
    # Audit rows are append-only for every application runtime.
    "audit_events": ("INSERT",),
}

# The worker can claim/complete jobs and publish the transactional outbox. It
# can validate approval/evidence snapshots, but it cannot create or alter them.
WORKER_TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "organizations": ("SELECT",),
    "design_versions": ("SELECT",),
    "generation_jobs": ("SELECT", "UPDATE"),
    "outbox_events": ("SELECT", "INSERT", "UPDATE"),
    "artifacts": ("SELECT", "INSERT"),
    "approvals": ("SELECT",),
    "external_evidence": ("SELECT",),
    "audit_events": ("INSERT",),
}


def _grant_table_privileges(
    role: str,
    table_privileges: dict[str, tuple[str, ...]],
) -> None:
    by_privilege_set: dict[tuple[str, ...], list[str]] = {}
    for table, privileges in table_privileges.items():
        by_privilege_set.setdefault(privileges, []).append(table)
    for privileges, tables in sorted(by_privilege_set.items()):
        op.execute(
            f"GRANT {', '.join(privileges)} ON TABLE "
            f"{', '.join(sorted(tables))} TO {role}"
        )


def _role_list(roles: Iterable[str]) -> str:
    return ", ".join(roles)


def upgrade() -> None:
    roles = _role_list(RUNTIME_ROLES)

    # The original bootstrap default granted both runtime roles full DML on
    # every future table. Revoke both the inherited defaults and all effective
    # table/sequence privileges before rebuilding the allow-list below.
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
        f"REVOKE ALL PRIVILEGES ON TABLES FROM {roles}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
        f"REVOKE ALL PRIVILEGES ON SEQUENCES FROM {roles}"
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {roles}")
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {roles}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {roles}")

    _grant_table_privileges("custombuild_api", API_TABLE_PRIVILEGES)
    _grant_table_privileges("custombuild_worker", WORKER_TABLE_PRIVILEGES)


def downgrade() -> None:
    roles = _role_list(RUNTIME_ROLES)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {roles}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {roles}")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {roles}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {roles}"
    )
