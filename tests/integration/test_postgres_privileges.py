from __future__ import annotations

import importlib
import os
import uuid
from collections.abc import Mapping

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError


def _urls() -> tuple[str, str, str, str]:
    bootstrap_url = os.getenv("TENANT_GRAPH_DATABASE_URL")
    migrator_url = os.getenv("MIGRATION_DATABASE_URL")
    api_url = os.getenv("RLS_DATABASE_URL")
    worker_url = os.getenv("WORKER_RLS_DATABASE_URL")
    if not all((bootstrap_url, migrator_url, api_url, worker_url)):
        pytest.skip("PostgreSQL privilege probes require all four database roles")
    return bootstrap_url, migrator_url, api_url, worker_url


def _direct_table_privileges(engine: Engine) -> dict[str, frozenset[str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT table_name, privilege_type "
                "FROM information_schema.role_table_grants "
                "WHERE table_schema = 'public' AND grantee = current_user "
                "ORDER BY table_name, privilege_type"
            )
        )
        privileges: dict[str, set[str]] = {}
        for table_name, privilege in rows:
            privileges.setdefault(str(table_name), set()).add(str(privilege))
    return {table: frozenset(values) for table, values in privileges.items()}


def _expected(mapping: Mapping[str, tuple[str, ...]]) -> dict[str, frozenset[str]]:
    return {table: frozenset(privileges) for table, privileges in mapping.items()}


def _assert_statement_denied(
    engine: Engine,
    statement: str,
    parameters: Mapping[str, object] | None = None,
    *,
    organization_id: str | None = None,
) -> None:
    with engine.connect() as connection:
        transaction = connection.begin()
        if organization_id is not None:
            connection.execute(
                text("SELECT set_config('app.current_organization_id', :tenant, true)"),
                {"tenant": organization_id},
            )
        with pytest.raises(DBAPIError, match="permission denied"):
            connection.execute(text(statement), parameters or {})
        transaction.rollback()


@pytest.mark.postgres
def test_runtime_roles_have_only_the_declared_table_privileges() -> None:
    _, _, api_url, worker_url = _urls()
    migration = importlib.import_module(
        "services.api.alembic.versions.0011_runtime_role_privileges"
    )
    api_engine = create_engine(api_url)
    worker_engine = create_engine(worker_url)
    try:
        assert _direct_table_privileges(api_engine) == _expected(
            migration.API_TABLE_PRIVILEGES
        )
        assert _direct_table_privileges(worker_engine) == _expected(
            migration.WORKER_TABLE_PRIVILEGES
        )
    finally:
        api_engine.dispose()
        worker_engine.dispose()


@pytest.mark.postgres
def test_runtime_roles_cannot_mutate_identity_or_append_only_audit_data() -> None:
    bootstrap_url, _, api_url, worker_url = _urls()
    suffix = uuid.uuid4().hex[:10]
    organization_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    audit_id = str(uuid.uuid4())
    bootstrap = create_engine(bootstrap_url)
    api = create_engine(api_url)
    worker = create_engine(worker_url)
    try:
        with bootstrap.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, created_at, updated_at) "
                    "VALUES (:id, 'Privilege tenant', :slug, now(), now())"
                ),
                {"id": organization_id, "slug": f"privilege-{suffix}"},
            )
            connection.execute(
                text(
                    "INSERT INTO users (id, oidc_sub, email, name, created_at, updated_at) "
                    "VALUES (:id, :subject, :email, 'Privilege user', now(), now())"
                ),
                {
                    "id": user_id,
                    "subject": f"privilege-user-{suffix}",
                    "email": f"privilege-{suffix}@example.test",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO projects "
                    "(id, organization_id, name, description, furniture_type, current_revision, "
                    "draft_revision, archived, created_at, updated_at) VALUES "
                    "(:id, :organization_id, 'Privilege project', '', 'bookcase', "
                    "0, 0, false, now(), now())"
                ),
                {"id": project_id, "organization_id": organization_id},
            )
            connection.execute(
                text(
                    "INSERT INTO audit_events "
                    "(id, organization_id, occurred_at, actor_id, action, entity_type, "
                    "entity_id, payload_json) VALUES "
                    "(:id, :organization_id, now(), :actor_id, 'fixture.created', "
                    "'project', :entity_id, '{}')"
                ),
                {
                    "id": audit_id,
                    "organization_id": organization_id,
                    "actor_id": user_id,
                    "entity_id": project_id,
                },
            )

        # API authentication may read global identities, but the runtime cannot
        # provision, rewrite or delete them.
        with api.begin() as connection:
            assert connection.execute(
                text("SELECT id FROM users WHERE id = :id"), {"id": user_id}
            ).scalar_one() == user_id
        _assert_statement_denied(
            api,
            "UPDATE users SET name = 'tampered' WHERE id = :id",
            {"id": user_id},
        )
        _assert_statement_denied(
            api,
            "DELETE FROM organizations WHERE id = :id",
            {"id": organization_id},
        )

        # Both runtimes may append tenant audit rows; neither can rewrite or
        # erase the historical record.
        for engine in (api, worker):
            with engine.connect() as connection:
                transaction = connection.begin()
                connection.execute(
                    text("SELECT set_config('app.current_organization_id', :tenant, true)"),
                    {"tenant": organization_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO audit_events "
                        "(id, organization_id, occurred_at, actor_id, action, entity_type, "
                        "entity_id, payload_json) VALUES "
                        "(:id, :organization_id, now(), :actor_id, 'privilege.probe', "
                        "'project', :entity_id, '{}')"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "organization_id": organization_id,
                        "actor_id": user_id,
                        "entity_id": project_id,
                    },
                )
                transaction.rollback()
            _assert_statement_denied(
                engine,
                "UPDATE audit_events SET action = 'tampered' WHERE id = :id",
                {"id": audit_id},
                organization_id=organization_id,
            )
            _assert_statement_denied(
                engine,
                "DELETE FROM audit_events WHERE id = :id",
                {"id": audit_id},
                organization_id=organization_id,
            )

        # A compromised worker cannot inspect global users or alter projects.
        _assert_statement_denied(
            worker,
            "SELECT id FROM users WHERE id = :id",
            {"id": user_id},
        )
        _assert_statement_denied(
            worker,
            "UPDATE projects SET name = 'tampered' WHERE id = :id",
            {"id": project_id},
            organization_id=organization_id,
        )
    finally:
        with bootstrap.begin() as connection:
            connection.execute(
                text("DELETE FROM organizations WHERE id = :id"),
                {"id": organization_id},
            )
            connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        api.dispose()
        worker.dispose()
        bootstrap.dispose()


@pytest.mark.postgres
def test_future_migrator_objects_are_not_auto_granted_to_runtime_roles() -> None:
    _, migrator_url, api_url, worker_url = _urls()
    migrator = create_engine(migrator_url)
    api = create_engine(api_url)
    worker = create_engine(worker_url)
    try:
        with migrator.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS runtime_privilege_future_table"))
            connection.execute(text("DROP SEQUENCE IF EXISTS runtime_privilege_future_sequence"))
            connection.execute(text("CREATE TABLE runtime_privilege_future_table (id integer)"))
            connection.execute(text("CREATE SEQUENCE runtime_privilege_future_sequence"))

        for engine in (api, worker):
            _assert_statement_denied(
                engine,
                "SELECT id FROM runtime_privilege_future_table",
            )
            _assert_statement_denied(
                engine,
                "SELECT nextval('runtime_privilege_future_sequence')",
            )
    finally:
        with migrator.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS runtime_privilege_future_table"))
            connection.execute(text("DROP SEQUENCE IF EXISTS runtime_privilege_future_sequence"))
        api.dispose()
        worker.dispose()
        migrator.dispose()
