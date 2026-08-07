from __future__ import annotations

import ast
from pathlib import Path

MIGRATIONS = Path("services/api/alembic/versions")
INITIAL_MIGRATION = MIGRATIONS / "0001_initial.py"
ENGINE_CONTEXT_MIGRATION = MIGRATIONS / "0002_generation_engine_context.py"


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
