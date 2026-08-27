"""Canonical PostgreSQL privileges for untrusted application runtimes.

This module deliberately has no application, SQLAlchemy or Alembic imports so
the migration, restore drill and live privilege probes can share one exact
allow-list without initializing runtime settings or model metadata.
"""

from __future__ import annotations

from collections.abc import Mapping

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

ROLE_TABLE_PRIVILEGES: dict[str, Mapping[str, tuple[str, ...]]] = {
    "custombuild_api": API_TABLE_PRIVILEGES,
    "custombuild_worker": WORKER_TABLE_PRIVILEGES,
}


def _grant_table_statements(
    role: str,
    table_privileges: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    by_privilege_set: dict[tuple[str, ...], list[str]] = {}
    for table, privileges in table_privileges.items():
        by_privilege_set.setdefault(privileges, []).append(table)
    return tuple(
        f"GRANT {', '.join(privileges)} ON TABLE "
        f"{', '.join(sorted(tables))} TO {role}"
        for privileges, tables in sorted(by_privilege_set.items())
    )


def runtime_privilege_statements() -> tuple[str, ...]:
    """Return the complete fail-closed runtime ACL rebuild in execution order."""

    roles = ", ".join(RUNTIME_ROLES)
    statements = [
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
            "REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC"
        ),
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
            "REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC"
        ),
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
            f"REVOKE ALL PRIVILEGES ON TABLES FROM {roles}"
        ),
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
            f"REVOKE ALL PRIVILEGES ON SEQUENCES FROM {roles}"
        ),
        "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC",
        "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC",
        f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {roles}",
        f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {roles}",
        f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {roles}",
        f"GRANT USAGE ON SCHEMA public TO {roles}",
    ]
    for role, table_privileges in ROLE_TABLE_PRIVILEGES.items():
        statements.extend(_grant_table_statements(role, table_privileges))
    return tuple(statements)


def runtime_privileges_sql() -> str:
    """Render the canonical ACL rebuild for psql-based recovery tooling."""

    return ";\n".join(runtime_privilege_statements()) + ";\n"
