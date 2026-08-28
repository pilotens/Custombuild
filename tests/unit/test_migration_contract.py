from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any

import pytest

from scripts.postgres_runtime_privileges import (
    API_TABLE_PRIVILEGES,
    WORKER_TABLE_PRIVILEGES,
    runtime_privilege_statements,
)

MIGRATIONS = Path("services/api/alembic/versions")
INITIAL_MIGRATION = MIGRATIONS / "0001_initial.py"
ENGINE_CONTEXT_MIGRATION = MIGRATIONS / "0002_generation_engine_context.py"
IMPORTED_ASSET_MIGRATION = MIGRATIONS / "0007_imported_reference_assets.py"
RUNTIME_RELIABILITY_MIGRATION = MIGRATIONS / "0008_runtime_reliability.py"
GENERATION_LEASE_HEARTBEAT_MIGRATION = (
    MIGRATIONS / "0009_generation_lease_heartbeat.py"
)
TENANT_GRAPH_MIGRATION = MIGRATIONS / "0010_tenant_graph_foreign_keys.py"
RUNTIME_ROLE_PRIVILEGES_MIGRATION = MIGRATIONS / "0011_runtime_role_privileges.py"
STORAGE_QUOTA_MIGRATION = MIGRATIONS / "0012_storage_quota_ledger.py"
TEMPLATE_CAPABILITY_MIGRATION = MIGRATIONS / "0005_template_capability_identity.py"


class _FakePreflightResult:
    def __init__(
        self,
        *,
        values: tuple[str, ...] = (),
        scalar: int = 0,
    ) -> None:
        self.values = values
        self.scalar = scalar

    def scalars(self) -> tuple[str, ...]:
        return self.values

    def scalar_one(self) -> int:
        return self.scalar


class _FakeTenantPreflightBind:
    def __init__(
        self,
        organization_ids: tuple[str, ...],
        *,
        mismatch: tuple[str, str] | None = None,
    ) -> None:
        self.organization_ids = organization_ids
        self.mismatch = mismatch
        self.current_tenant = ""
        self.contexts: list[str] = []
        self.relationship_queries: list[tuple[str, str]] = []

    def execute(
        self,
        statement: object,
        parameters: dict[str, str] | None = None,
    ) -> _FakePreflightResult:
        sql = str(statement)
        if sql == "SELECT id FROM organizations ORDER BY id":
            return _FakePreflightResult(values=self.organization_ids)
        if "set_config('app.current_organization_id'" in sql:
            self.current_tenant = parameters["organization_id"] if parameters else ""
            self.contexts.append(self.current_tenant)
            return _FakePreflightResult()
        assert self.current_tenant in self.organization_ids
        self.relationship_queries.append((self.current_tenant, sql))
        mismatch_count = int(
            self.mismatch is not None
            and self.current_tenant == self.mismatch[0]
            and self.mismatch[1] in sql
        )
        return _FakePreflightResult(scalar=mismatch_count)


def _tenant_graph_module() -> Any:
    return importlib.import_module(
        "services.api.alembic.versions.0010_tenant_graph_foreign_keys"
    )


def test_initial_migration_does_not_depend_on_live_application_metadata() -> None:
    source = INITIAL_MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(source)

    application_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    application_imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    metadata_operations = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert not {name for name in application_imports if name == "app" or name.startswith("app.")}
    assert "create_all" not in metadata_operations
    assert "drop_all" not in metadata_operations


def test_engine_context_column_belongs_to_second_revision() -> None:
    initial_source = INITIAL_MIGRATION.read_text(encoding="utf-8")
    engine_context_source = ENGINE_CONTEXT_MIGRATION.read_text(encoding="utf-8")

    assert "production_engine_context_json" not in initial_source
    assert "production_engine_context_json" in engine_context_source


def test_reference_assets_are_project_bound_rls_protected_and_append_only() -> None:
    source = IMPORTED_ASSET_MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision = "0006_external_evidence"' in source
    assert "ALTER TABLE imported_assets ENABLE ROW LEVEL SECURITY" in source
    assert "ALTER TABLE imported_assets FORCE ROW LEVEL SECURITY" in source
    assert "GRANT SELECT, INSERT ON imported_assets" in source
    assert "GRANT SELECT, INSERT, UPDATE" not in source
    assert '["organization_id", "project_id", "source_import_id"]' in source
    assert '["organization_id", "project_id", "id"]' in source


def test_descriptive_revision_ids_fit_the_widened_alembic_version_column() -> None:
    source = TEMPLATE_CAPABILITY_MIGRATION.read_text(encoding="utf-8")

    assert '"alembic_version"' in source
    assert 'type_=sa.String(length=128)' in source


def test_runtime_reliability_columns_are_added_after_reference_assets() -> None:
    source = RUNTIME_RELIABILITY_MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision = "0007_imported_reference_assets"' in source
    assert '"draft_revision"' in source
    assert 'server_default=sa.text("0")' in source
    assert '"lease_token"' in source
    assert '"dead_lettered_at"' in source
    assert '"last_error"' in source


def test_generation_heartbeat_lease_is_migrated_after_runtime_reliability() -> None:
    source = GENERATION_LEASE_HEARTBEAT_MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision = "0008_runtime_reliability"' in source
    assert '"lease_expires_at"' in source
    assert '"deadline_at"' in source
    assert '"ix_generation_jobs_status_lease_expires_at"' in source
    assert "INTERVAL '30 minutes'" in source
    assert "INTERVAL '2 hours'" in source


def test_tenant_graph_migration_preflights_and_replaces_every_parent_edge() -> None:
    source = TENANT_GRAPH_MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision = "0009_generation_lease_heartbeat"' in source
    assert "TENANT_GRAPH_PRECHECK_FAILED" in source
    assert "parent.id IS NULL" in source
    assert "child.organization_id <> parent.organization_id" in source
    assert '"projects", "uq_projects_org_id"' in source
    assert '"design_versions", "uq_design_versions_org_id"' in source
    assert '"generation_jobs", "uq_generation_jobs_org_id"' in source
    assert "PRODUCTION_TABLES" in source
    assert '"fk_approvals_org_generation_job"' in source
    assert "_restore_single_column_foreign_key" in source


def test_runtime_role_migration_revokes_blanket_defaults_before_explicit_grants() -> None:
    source = RUNTIME_ROLE_PRIVILEGES_MIGRATION.read_text(encoding="utf-8")
    statements = runtime_privilege_statements()
    privilege_sql = ";\n".join(statements)

    assert 'down_revision = "0010_tenant_graph_foreign_keys"' in source
    assert "FROZEN_PRIVILEGE_STATEMENTS" in source
    assert "scripts.postgres_runtime_privileges" not in source
    assert "storage_global_quotas" not in source
    assert "storage_tenant_quotas" not in source
    assert "stored_objects" not in source
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public" in privilege_sql
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public" in privilege_sql
    assert "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public" in privilege_sql
    assert "REVOKE ALL PRIVILEGES ON TABLES" in privilege_sql
    assert "REVOKE ALL PRIVILEGES ON SEQUENCES" in privilege_sql
    assert "FROM PUBLIC" in privilege_sql
    assert "organizations" not in API_TABLE_PRIVILEGES
    assert API_TABLE_PRIVILEGES["users"] == ("SELECT",)
    assert API_TABLE_PRIVILEGES["outbox_events"] == ("INSERT",)
    assert API_TABLE_PRIVILEGES["audit_events"] == ("INSERT",)
    assert WORKER_TABLE_PRIVILEGES["generation_jobs"] == (
        "SELECT",
        "UPDATE",
    )
    assert WORKER_TABLE_PRIVILEGES["approvals"] == ("SELECT",)
    assert WORKER_TABLE_PRIVILEGES["audit_events"] == ("INSERT",)
    assert "users" not in WORKER_TABLE_PRIVILEGES
    assert "memberships" not in WORKER_TABLE_PRIVILEGES
    assert "projects" not in WORKER_TABLE_PRIVILEGES
    assert "releases" not in WORKER_TABLE_PRIVILEGES


def test_storage_quota_migration_backfills_before_reference_constraints() -> None:
    source = STORAGE_QUOTA_MIGRATION.read_text(encoding="utf-8")
    tombstone_ddl = source.split(
        'op.create_table(\n        "storage_object_tombstones"', 1
    )[1].split('op.create_table(\n        "stored_objects"', 1)[0]

    assert 'down_revision = "0011_runtime_role_privileges"' in source
    assert "STORAGE_QUOTA_BACKFILL_FAILED" in source
    assert "GLOBAL_STORAGE_BYTE_LIMIT" not in source
    assert "TENANT_STORAGE_BYTE_LIMIT" in source
    assert '"capacity_verified"' in source
    assert "0, :committed_count, false" in source
    assert '"provisioned_bytes"' in source
    assert '"capacity_evidence_sha256"' in source
    assert "max(global_bytes, 1)" in source
    assert "reserved_bytes <= byte_limit - committed_bytes" in source
    assert "reserved_count <= object_limit - committed_count" in source
    assert "ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY" in source
    assert "ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY" in source
    assert "current_setting('app.current_organization_id', true)" in source
    assert "'delete_pending'" in source
    assert "'reaping'" in source
    assert 'sa.Column("claim_token"' in source
    assert "artifact.kind || ':' || artifact.id" in source
    assert "global_object_keys: set[str] = set()" in source
    assert "duplicate physical legacy" in source
    assert 'sa.PrimaryKeyConstraint("capacity_bucket", "object_key")' in tombstone_ddl
    assert 'name="uq_storage_tombstones_bucket_idempotency_key"' in tombstone_ddl
    assert "ForeignKeyConstraint" not in tombstone_ddl
    assert 'name="uq_stored_objects_global_object_key"' in source
    # Revision 0012 creates and backfills the ledger without exposing mutable
    # table privileges. Revision 0013 installs the reviewed function-only ACL.
    assert "runtime_privilege_statements" not in source
    assert source.index("_backfill_existing_objects()") < source.index(
        '"fk_imported_assets_stored_object"',
        source.index("def upgrade()"),
    )
    assert source.count('ondelete="RESTRICT"') >= 4


def test_current_runtime_allowlist_adds_only_required_storage_ledger_access() -> None:
    expected = {
        "storage_global_quotas": ("SELECT",),
        "storage_tenant_quotas": ("SELECT",),
        "stored_objects": ("SELECT",),
    }

    for table, privileges in expected.items():
        assert API_TABLE_PRIVILEGES[table] == privileges
        assert WORKER_TABLE_PRIVILEGES[table] == privileges


def test_fresh_upgrade_revision_0011_never_mentions_revision_0012_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module(
        "services.api.alembic.versions.0011_runtime_role_privileges"
    )
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda statement: statements.append(statement))

    migration.upgrade()

    sql = ";\n".join(statements)
    assert "storage_global_quotas" not in sql
    assert "storage_tenant_quotas" not in sql
    assert "stored_objects" not in sql


def test_postgres_bootstrap_never_auto_grants_future_runtime_tables() -> None:
    source = Path("infra/postgres/init-roles.sh").read_text(encoding="utf-8")

    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES" not in source
    assert "GRANT USAGE, SELECT ON SEQUENCES" not in source
    assert "REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC" in source
    assert "REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC" in source
    assert "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC" in source
    assert "REVOKE ALL PRIVILEGES ON SCHEMA public" in source
    assert (
        "REVOKE ALL PRIVILEGES ON TABLES FROM custombuild_api, custombuild_worker"
        in source
    )
    assert (
        "REVOKE ALL PRIVILEGES ON SEQUENCES FROM custombuild_api, custombuild_worker"
        in source
    )
    assert (
        "REVOKE EXECUTE ON FUNCTIONS FROM custombuild_api, custombuild_worker"
        in source
    )


def test_tenant_graph_preflight_checks_every_edge_per_tenant_and_resets_rls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _tenant_graph_module()
    bind = _FakeTenantPreflightBind(("tenant-a", "tenant-b"))
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)

    migration._preflight_existing_rows()

    assert len(migration.TENANT_RELATIONSHIPS) == 25
    assert len(bind.relationship_queries) == 50
    assert bind.contexts == ["tenant-a", "tenant-b", ""]
    for tenant in ("tenant-a", "tenant-b"):
        tenant_queries = [sql for current, sql in bind.relationship_queries if current == tenant]
        for relationship in migration.TENANT_RELATIONSHIPS:
            assert any(
                f"FROM {relationship.child_table} AS child" in sql
                and f"LEFT JOIN {relationship.parent_table} AS parent" in sql
                and f"child.{relationship.child_column}" in sql
                for sql in tenant_queries
            )


def test_tenant_graph_preflight_clears_rls_context_for_empty_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _tenant_graph_module()
    bind = _FakeTenantPreflightBind(())
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)

    migration._preflight_existing_rows()

    assert bind.relationship_queries == []
    assert bind.contexts == [""]


def test_tenant_graph_mismatch_aborts_upgrade_before_ddl_and_resets_rls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _tenant_graph_module()
    bind = _FakeTenantPreflightBind(
        ("tenant-a", "tenant-b"),
        mismatch=("tenant-b", "FROM designs AS child"),
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)

    def unexpected_ddl(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("tenant graph DDL ran before the preflight passed")

    monkeypatch.setattr(migration.op, "create_unique_constraint", unexpected_ddl)
    monkeypatch.setattr(migration, "_replace_with_tenant_foreign_key", unexpected_ddl)

    with pytest.raises(
        RuntimeError,
        match=r"TENANT_GRAPH_PRECHECK_FAILED: designs\.project_id -> projects\.id",
    ):
        migration.upgrade()

    assert len(bind.relationship_queries) == 50
    assert bind.contexts == ["tenant-a", "tenant-b", ""]
