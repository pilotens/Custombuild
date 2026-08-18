from __future__ import annotations

import ast
from pathlib import Path

MIGRATIONS = Path("services/api/alembic/versions")
INITIAL_MIGRATION = MIGRATIONS / "0001_initial.py"
ENGINE_CONTEXT_MIGRATION = MIGRATIONS / "0002_generation_engine_context.py"
IMPORTED_ASSET_MIGRATION = MIGRATIONS / "0007_imported_reference_assets.py"
RUNTIME_RELIABILITY_MIGRATION = MIGRATIONS / "0008_runtime_reliability.py"
GENERATION_LEASE_HEARTBEAT_MIGRATION = (
    MIGRATIONS / "0009_generation_lease_heartbeat.py"
)
TEMPLATE_CAPABILITY_MIGRATION = MIGRATIONS / "0005_template_capability_identity.py"


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
