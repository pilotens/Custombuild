"""Canonical PostgreSQL privileges for untrusted application runtimes.

This module deliberately has no application, SQLAlchemy or Alembic imports so
the migration, restore drill and live privilege probes can share one exact
allow-list without initializing runtime settings or model metadata.
"""

from __future__ import annotations

from collections.abc import Mapping

RUNTIME_ROLES = ("custombuild_api", "custombuild_worker")
STORAGE_ATTESTOR_ROLE = "custombuild_storage_attestor"

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
    # The storage ledger is mutated only through fixed-search-path,
    # SECURITY DEFINER functions.  A compromised runtime may inspect its
    # effective accounting state but can never rewrite limits, counters,
    # immutable identities or leases directly.
    "storage_global_quotas": ("SELECT",),
    "storage_tenant_quotas": ("SELECT",),
    "stored_objects": ("SELECT",),
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
    "storage_global_quotas": ("SELECT",),
    "storage_tenant_quotas": ("SELECT",),
    "stored_objects": ("SELECT",),
    "approvals": ("SELECT",),
    "external_evidence": ("SELECT",),
    "audit_events": ("INSERT",),
}

ROLE_TABLE_PRIVILEGES: dict[str, Mapping[str, tuple[str, ...]]] = {
    "custombuild_api": API_TABLE_PRIVILEGES,
    "custombuild_worker": WORKER_TABLE_PRIVILEGES,
}
STORAGE_ATTESTOR_TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "organizations": ("SELECT",),
    "storage_global_quotas": ("SELECT",),
    "storage_tenant_quotas": ("SELECT",),
    "stored_objects": ("SELECT",),
    # Retired keys are globally scoped and immutable. Only the attestor needs
    # read access to prove that no live ledger key overlaps the append-only
    # registry; API and worker reach it solely through SECURITY DEFINER code.
    "storage_object_tombstones": ("SELECT",),
}

STORAGE_QUOTA_MUTATOR_SIGNATURES = (
    "public.custombuild_storage_reserve_batch(text, jsonb, text, integer)",
    "public.custombuild_storage_renew_batch(text, jsonb, text, integer)",
    "public.custombuild_storage_commit_batch(text, jsonb, text)",
)
STORAGE_API_RETRY_SIGNATURES = ("public.custombuild_storage_prepare_generation_retry(text, text)",)
STORAGE_REAPER_SIGNATURES = (
    "public.custombuild_storage_assert_reap_bucket(text)",
    "public.custombuild_storage_claim_expired_reservations(text, text, integer, integer)",
    "public.custombuild_storage_claim_delete_pending(text, text, integer, integer)",
    "public.custombuild_storage_finalize_reap(text, text, text, bigint, text, text)",
)
STORAGE_ATTESTOR_SIGNATURES = (
    "public.custombuild_storage_lock_capacity()",
    (
        "public.custombuild_storage_attest_capacity(bigint, bigint, bigint, bigint, "
        "bigint, text, text, text, text, text, bigint, bigint, bigint, bigint, "
        "timestamptz, text)"
    ),
    "public.custombuild_storage_invalidate_capacity(text)",
)
ROLE_FUNCTION_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "custombuild_api": STORAGE_QUOTA_MUTATOR_SIGNATURES + STORAGE_API_RETRY_SIGNATURES,
    "custombuild_worker": STORAGE_QUOTA_MUTATOR_SIGNATURES + STORAGE_REAPER_SIGNATURES,
}


def _grant_table_statements(
    role: str,
    table_privileges: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    by_privilege_set: dict[tuple[str, ...], list[str]] = {}
    for table, privileges in table_privileges.items():
        by_privilege_set.setdefault(privileges, []).append(table)
    return tuple(
        f"GRANT {', '.join(privileges)} ON TABLE {', '.join(sorted(tables))} TO {role}"
        for privileges, tables in sorted(by_privilege_set.items())
    )


def runtime_privilege_statements() -> tuple[str, ...]:
    """Return the complete fail-closed runtime ACL rebuild in execution order."""

    roles = ", ".join(RUNTIME_ROLES)
    all_untrusted_roles = f"{roles}, {STORAGE_ATTESTOR_ROLE}"
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
            f"REVOKE ALL PRIVILEGES ON TABLES FROM {all_untrusted_roles}"
        ),
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
            f"REVOKE ALL PRIVILEGES ON SEQUENCES FROM {all_untrusted_roles}"
        ),
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator "
            "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
        ),
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator "
            f"REVOKE EXECUTE ON FUNCTIONS FROM {all_untrusted_roles}"
        ),
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
            "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
        ),
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
            f"REVOKE EXECUTE ON FUNCTIONS FROM {all_untrusted_roles}"
        ),
        "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC",
        "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC",
        "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC",
        f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {all_untrusted_roles}",
        f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {all_untrusted_roles}",
        f"REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM {all_untrusted_roles}",
        f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {all_untrusted_roles}",
        f"GRANT USAGE ON SCHEMA public TO {all_untrusted_roles}",
    ]
    for role, table_privileges in ROLE_TABLE_PRIVILEGES.items():
        statements.extend(_grant_table_statements(role, table_privileges))
    statements.extend(
        _grant_table_statements(
            STORAGE_ATTESTOR_ROLE,
            STORAGE_ATTESTOR_TABLE_PRIVILEGES,
        )
    )
    return tuple(statements)


def storage_quota_function_privilege_statements() -> tuple[str, ...]:
    """Return grants for mutators created after the base ACL is rebuilt."""

    runtime_grants = tuple(
        f"GRANT EXECUTE ON FUNCTION {signature} TO {role}"
        for role, signatures in ROLE_FUNCTION_PRIVILEGES.items()
        for signature in signatures
    )
    attestor_grants = tuple(
        f"GRANT EXECUTE ON FUNCTION {signature} TO {STORAGE_ATTESTOR_ROLE}"
        for signature in STORAGE_ATTESTOR_SIGNATURES
    )
    return (*runtime_grants, *attestor_grants)


def runtime_privileges_sql() -> str:
    """Render the canonical ACL rebuild for psql-based recovery tooling."""

    statements = (
        *runtime_privilege_statements(),
        *storage_quota_function_privilege_statements(),
    )
    return ";\n".join(statements) + ";\n"
