from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Mapping

import pytest
from sqlalchemy import Engine, bindparam, create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

WORKSHOP_TABLES = (
    "workshop_trust_states",
    "workshop_actors",
    "workshop_signer_principals",
    "workshop_issuer_keys",
    "workshop_policies",
    "workshop_runs",
    "workshop_run_programs",
    "workshop_nonce_sets",
    "workshop_nonces",
    "workshop_chain_acceptances",
    "workshop_acceptance_signers",
    "workshop_revocations",
)

IMMUTABLE_WORKSHOP_TABLES = set(WORKSHOP_TABLES) - {
    "workshop_trust_states",
    "workshop_nonce_sets",
}

_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)


def _database_url(name: str) -> str:
    value = os.getenv(name)
    if not value:
        if os.getenv("CI", "").lower() == "true":
            pytest.fail(
                f"PostgreSQL workshop persistence probe requires {name} in CI"
            )
        pytest.skip(f"PostgreSQL workshop persistence probe requires {name}")
    return value


def _engine(name: str) -> Engine:
    return create_engine(
        _database_url(name),
        pool_pre_ping=True,
        poolclass=NullPool,
    )


def _set_tenant(connection: Connection, organization_id: str) -> None:
    connection.execute(
        text("SELECT set_config('app.current_organization_id', :tenant, true)"),
        {"tenant": organization_id},
    )


def _current_tenant(connection: Connection) -> str | None:
    value = connection.execute(
        text("SELECT current_setting('app.current_organization_id', true)")
    ).scalar_one_or_none()
    return str(value) if value is not None else None


def _insert_organization(
    connection: Connection,
    *,
    organization_id: str,
    slug: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO organizations (id, name, slug, created_at, updated_at) "
            "VALUES (:id, :name, :slug, clock_timestamp(), clock_timestamp())"
        ),
        {
            "id": organization_id,
            "name": f"Workshop persistence {slug}",
            "slug": slug,
        },
    )


def _effective_privileges(connection: Connection, table_name: str) -> dict[str, bool]:
    expressions = ", ".join(
        f"has_table_privilege(current_user, :table_name, '{privilege}') "
        f"AS {privilege.lower()}"
        for privilege in _PRIVILEGES
    )
    row = connection.execute(
        text(f"SELECT {expressions}"),  # noqa: S608 - expressions are frozen above.
        {"table_name": f"public.{table_name}"},
    ).mappings().one()
    return {privilege: bool(row[privilege.lower()]) for privilege in _PRIVILEGES}


def _assert_statement_rejected(
    connection: Connection,
    statement: str,
    parameters: Mapping[str, object] | None = None,
    *,
    message: str,
) -> None:
    savepoint = connection.begin_nested()
    try:
        with pytest.raises(DBAPIError) as caught:
            connection.execute(text(statement), parameters or {})
        assert message in str(caught.value.orig)
    finally:
        savepoint.rollback()


@pytest.mark.postgres
def test_workshop_tables_force_rls_and_install_all_immutable_triggers() -> None:
    engine = _engine("TENANT_GRAPH_DATABASE_URL")
    table_query = text(
        """
        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname IN :table_names
        ORDER BY c.relname
        """
    ).bindparams(bindparam("table_names", expanding=True))
    trigger_query = text(
        """
        SELECT c.relname, t.tgname, t.tgenabled
        FROM pg_catalog.pg_trigger AS t
        JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND NOT t.tgisinternal
          AND c.relname IN :table_names
        ORDER BY c.relname, t.tgname
        """
    ).bindparams(bindparam("table_names", expanding=True))

    try:
        with engine.connect() as connection:
            rows = connection.execute(
                table_query,
                {"table_names": WORKSHOP_TABLES},
            ).mappings().all()
            assert {str(row["relname"]) for row in rows} == set(WORKSHOP_TABLES)
            assert all(
                bool(row["relrowsecurity"]) and bool(row["relforcerowsecurity"])
                for row in rows
            )

            triggers = connection.execute(
                trigger_query,
                {"table_names": WORKSHOP_TABLES},
            ).mappings().all()
            immutable_triggers = {
                str(row["relname"])
                for row in triggers
                if str(row["tgname"]).endswith("_reject_mutation")
                and str(row["tgenabled"]) != "D"
            }
            assert immutable_triggers == IMMUTABLE_WORKSHOP_TABLES

            organization_trigger = connection.execute(
                text(
                    """
                    SELECT t.tgenabled
                    FROM pg_catalog.pg_trigger AS t
                    JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relname = 'organizations'
                      AND t.tgname = 'workshop_initialize_trust_state'
                      AND NOT t.tgisinternal
                    """
                )
            ).scalar_one()
            assert str(organization_trigger) != "D"
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_workshop_runtime_acl_denies_both_untrusted_runtimes() -> None:
    api_engine = _engine("RLS_DATABASE_URL")
    worker_engine = _engine("WORKER_RLS_DATABASE_URL")
    try:
        with api_engine.connect() as connection:
            for table_name in WORKSHOP_TABLES:
                assert not any(_effective_privileges(connection, table_name).values())
            with pytest.raises(DBAPIError, match="permission denied"):
                connection.execute(text("SELECT 1 FROM workshop_trust_states LIMIT 0"))
            connection.rollback()

        with worker_engine.connect() as connection:
            for table_name in WORKSHOP_TABLES:
                assert not any(_effective_privileges(connection, table_name).values())
            with pytest.raises(DBAPIError, match="permission denied"):
                connection.execute(text("SELECT 1 FROM workshop_trust_states LIMIT 0"))
            connection.rollback()
    finally:
        api_engine.dispose()
        worker_engine.dispose()


@pytest.mark.postgres
def test_trust_state_trigger_restores_context_after_success_and_failure() -> None:
    engine = _engine("TENANT_GRAPH_DATABASE_URL")
    suffix = uuid.uuid4().hex[:12]
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    org_savepoint = str(uuid.uuid4())
    org_absent = str(uuid.uuid4())
    failure_function = f"workshop_test_trust_failure_{suffix}"
    failure_trigger = f"workshop_test_trust_failure_{suffix}"

    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                # NullPool gives this probe a new backend.  Prove the function
                # also handles a genuinely absent custom GUC, then restores the
                # fail-closed empty value after its temporary tenant binding.
                assert _current_tenant(connection) is None
                _insert_organization(
                    connection,
                    organization_id=org_a,
                    slug=f"workshop-pg-a-{suffix}",
                )
                assert _current_tenant(connection) == ""

                _set_tenant(connection, org_a)
                _insert_organization(
                    connection,
                    organization_id=org_b,
                    slug=f"workshop-pg-b-{suffix}",
                )
                assert _current_tenant(connection) == org_a

                connection.execute(
                    text(
                        "SELECT set_config("
                        "'test.workshop_trust_failure_org', :organization_id, true)"
                    ),
                    {"organization_id": org_savepoint},
                )
                connection.execute(
                    text(  # noqa: S608 - suffix is random lowercase hexadecimal.
                        f"""
                        CREATE FUNCTION public.{failure_function}()
                        RETURNS trigger
                        LANGUAGE plpgsql
                        SECURITY INVOKER
                        SET search_path TO pg_catalog, public
                        AS $function$
                        BEGIN
                          IF NEW.organization_id::text = current_setting(
                            'test.workshop_trust_failure_org', true
                          ) THEN
                            RAISE EXCEPTION USING
                              ERRCODE = 'P0001',
                              MESSAGE = 'WORKSHOP_TEST_TRUST_STATE_FAILURE';
                          END IF;
                          RETURN NEW;
                        END;
                        $function$
                        """
                    )
                )
                connection.execute(
                    text(  # noqa: S608 - suffix is random lowercase hexadecimal.
                        f"""
                        CREATE TRIGGER {failure_trigger}
                        BEFORE INSERT ON workshop_trust_states
                        FOR EACH ROW
                        EXECUTE FUNCTION public.{failure_function}()
                        """
                    )
                )

                savepoint = connection.begin_nested()
                try:
                    with pytest.raises(DBAPIError) as caught:
                        _insert_organization(
                            connection,
                            organization_id=org_savepoint,
                            slug=f"workshop-pg-sp-{suffix}",
                        )
                    assert "WORKSHOP_TEST_TRUST_STATE_FAILURE" in str(caught.value.orig)
                finally:
                    savepoint.rollback()
                assert _current_tenant(connection) == org_a
                assert connection.execute(
                    text("SELECT count(*) FROM organizations WHERE id = :id"),
                    {"id": org_savepoint},
                ).scalar_one() == 0
                assert connection.execute(
                    text(
                        "SELECT count(*) FROM workshop_trust_states "
                        "WHERE organization_id = :id"
                    ),
                    {"id": org_savepoint},
                ).scalar_one() == 0
                connection.execute(
                    text(
                        f"DROP TRIGGER {failure_trigger} "  # noqa: S608
                        "ON workshop_trust_states"
                    )
                )
                connection.execute(
                    text(
                        f"DROP FUNCTION public.{failure_function}()"  # noqa: S608
                    )
                )

                _set_tenant(connection, "")
                _insert_organization(
                    connection,
                    organization_id=org_absent,
                    slug=f"workshop-pg-none-{suffix}",
                )
                assert not _current_tenant(connection)
                assert set(
                    connection.execute(
                        text(
                            "SELECT organization_id FROM workshop_trust_states "
                            "WHERE organization_id IN (:org_a, :org_b, :org_absent)"
                        ),
                        {
                            "org_a": org_a,
                            "org_b": org_b,
                            "org_absent": org_absent,
                        },
                    ).scalars()
                ) == {org_a, org_b, org_absent}

            finally:
                if transaction.is_active:
                    transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_immutable_workshop_row_rejects_update_and_delete() -> None:
    engine = _engine("TENANT_GRAPH_DATABASE_URL")
    suffix = uuid.uuid4().hex[:12]
    organization_id = str(uuid.uuid4())
    actor_id = str(uuid.uuid4())

    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                _insert_organization(
                    connection,
                    organization_id=organization_id,
                    slug=f"workshop-pg-immutable-{suffix}",
                )
                _set_tenant(connection, organization_id)
                connection.execute(
                    text(
                        "INSERT INTO workshop_actors ("
                        "id, organization_id, actor_type, user_id, external_authority, "
                        "external_subject_sha256, created_at) VALUES ("
                        ":id, :organization_id, 'EXTERNAL_CERTIFIED_PERSON', NULL, "
                        ":authority, :subject_sha256, clock_timestamp())"
                    ),
                    {
                        "id": actor_id,
                        "organization_id": organization_id,
                        "authority": f"test-authority-{suffix}",
                        "subject_sha256": hashlib.sha256(suffix.encode()).hexdigest(),
                    },
                )

                _assert_statement_rejected(
                    connection,
                    "UPDATE workshop_actors SET external_authority = :authority "
                    "WHERE organization_id = :organization_id AND id = :id",
                    {
                        "authority": f"changed-{suffix}",
                        "organization_id": organization_id,
                        "id": actor_id,
                    },
                    message="WORKSHOP_IMMUTABLE_ROW",
                )
                _assert_statement_rejected(
                    connection,
                    "DELETE FROM workshop_actors "
                    "WHERE organization_id = :organization_id AND id = :id",
                    {"organization_id": organization_id, "id": actor_id},
                    message="WORKSHOP_IMMUTABLE_ROW",
                )
                assert connection.execute(
                    text(
                        "SELECT external_authority FROM workshop_actors "
                        "WHERE organization_id = :organization_id AND id = :id"
                    ),
                    {"organization_id": organization_id, "id": actor_id},
                ).scalar_one() == f"test-authority-{suffix}"
            finally:
                if transaction.is_active:
                    transaction.rollback()
    finally:
        engine.dispose()
