from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from scripts.postgres_runtime_privileges import (
    API_TABLE_PRIVILEGES,
    WORKER_TABLE_PRIVILEGES,
)
from scripts.storage_recovery import (
    PostgresRecoveryStore,
    StorageRecoveryError,
    recover_storage,
)
from services.api.app.storage_reaper import (
    StorageReapClaim,
    StorageReaperInvariantError,
    StorageReaperS3Client,
    StorageReapResult,
    reap_storage_claim,
)


def _urls() -> tuple[str, str, str, str]:
    bootstrap_url = os.getenv("TENANT_GRAPH_DATABASE_URL")
    migrator_url = os.getenv("MIGRATION_DATABASE_URL")
    api_url = os.getenv("RLS_DATABASE_URL")
    worker_url = os.getenv("WORKER_RLS_DATABASE_URL")
    if not all((bootstrap_url, migrator_url, api_url, worker_url)):
        if os.getenv("CI", "").lower() == "true":
            pytest.fail(
                "PostgreSQL privilege probes are mandatory in CI and require "
                "TENANT_GRAPH_DATABASE_URL, MIGRATION_DATABASE_URL, RLS_DATABASE_URL "
                "and WORKER_RLS_DATABASE_URL"
            )
        pytest.skip("PostgreSQL privilege probes require all four database roles")
    return bootstrap_url, migrator_url, api_url, worker_url


@dataclass(frozen=True)
class _StorageProbe:
    connection: Connection
    organization_id: str
    project_id: str


@pytest.fixture
def storage_probe() -> Iterator[_StorageProbe]:
    """Create isolated storage state that is always rolled back as one transaction."""

    bootstrap_url, _, _, _ = _urls()
    suffix = uuid.uuid4().hex[:10]
    organization_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    engine = create_engine(bootstrap_url)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, created_at, updated_at) "
                    "VALUES (:id, 'Storage privilege tenant', :slug, now(), now())"
                ),
                {"id": organization_id, "slug": f"storage-privilege-{suffix}"},
            )
            connection.execute(
                text(
                    "INSERT INTO projects "
                    "(id, organization_id, name, description, furniture_type, "
                    "current_revision, draft_revision, archived, created_at, updated_at) "
                    "VALUES (:id, :organization_id, 'Storage privilege project', '', "
                    "'bookcase', 0, 0, false, now(), now())"
                ),
                {"id": project_id, "organization_id": organization_id},
            )
            yield _StorageProbe(
                connection=connection,
                organization_id=organization_id,
                project_id=project_id,
            )
        finally:
            if transaction.is_active:
                transaction.rollback()
    engine.dispose()


_ROLE_STATEMENTS = {
    "custombuild_migrator": "SET LOCAL ROLE custombuild_migrator",
    "custombuild_api": "SET LOCAL ROLE custombuild_api",
    "custombuild_worker": "SET LOCAL ROLE custombuild_worker",
    "custombuild_storage_attestor": "SET LOCAL ROLE custombuild_storage_attestor",
}


def _set_local_role(connection: Connection, role: str | None) -> None:
    connection.execute(text("RESET ROLE"))
    if role is not None:
        connection.execute(text(_ROLE_STATEMENTS[role]))


def _set_tenant(connection: Connection, organization_id: str) -> None:
    connection.execute(
        text("SELECT set_config('app.current_organization_id', :tenant, true)"),
        {"tenant": organization_id},
    )


def _assert_connection_error(
    connection: Connection,
    statement: str,
    parameters: Mapping[str, object] | None = None,
    *,
    match: str,
) -> None:
    savepoint = connection.begin_nested()
    try:
        with pytest.raises(DBAPIError, match=match):
            connection.execute(text(statement), parameters or {})
    finally:
        savepoint.rollback()


def _global_storage_counters(connection: Connection) -> tuple[int, int, int, int]:
    row = connection.execute(
        text(
            "SELECT committed_bytes, committed_count, reserved_bytes, reserved_count "
            "FROM storage_global_quotas WHERE id = 1"
        )
    ).one()
    return tuple(int(value) for value in row)  # type: ignore[return-value]


def _attest_current_capacity(
    connection: Connection,
    *,
    attested_at: str = "clock_timestamp()",
    bucket: str = "custombuild-artifacts",
) -> None:
    _set_local_role(connection, None)
    committed_bytes, committed_count, reserved_bytes, reserved_count = _global_storage_counters(
        connection
    )
    byte_limit = committed_bytes + reserved_bytes + 1_000_000
    object_limit = committed_count + reserved_count + 1_000
    _set_local_role(connection, "custombuild_storage_attestor")
    connection.execute(
        text(
            "SELECT public.custombuild_storage_attest_capacity("
            ":provisioned_bytes, 1000000, 1000000, :byte_limit, :object_limit, "
            "'integration-volume', :bucket, :operator_hash, "
            ":deploy_hash, :inventory_hash, :committed_count, :committed_bytes, "
            ":committed_count, :committed_bytes, "
            f"{attested_at}, :evidence_hash)"
        ),
        {
            "provisioned_bytes": byte_limit + 2_000_000,
            "byte_limit": byte_limit,
            "object_limit": object_limit,
            "bucket": bucket,
            "operator_hash": "a" * 64,
            "deploy_hash": "b" * 64,
            "inventory_hash": "c" * 64,
            "committed_count": committed_count,
            "committed_bytes": committed_bytes,
            "evidence_hash": "d" * 64,
        },
    )


def _claim(
    probe: _StorageProbe,
    *,
    label: str,
    size_bytes: int,
) -> dict[str, object]:
    return {
        "project_id": probe.project_id,
        "object_key": f"integration/{probe.organization_id}/{label}.bin",
        "sha256": label[0] * 64,
        "size_bytes": size_bytes,
        "media_type": "application/octet-stream",
        "owner_type": "integration_probe",
        "owner_id": str(uuid.uuid4()),
        "idempotency_key": f"integration:{probe.organization_id}:{label}",
    }


def _call_storage_batch(
    connection: Connection,
    function_name: str,
    organization_id: str,
    claims: list[dict[str, object]],
    lease_token: str,
    lease_duration_seconds: int | None = None,
) -> object:
    parameters: dict[str, object] = {
        "organization_id": organization_id,
        "claims": json.dumps(claims, separators=(",", ":"), sort_keys=True),
        "lease_token": lease_token,
    }
    if lease_duration_seconds is None:
        statements = {
            "custombuild_storage_commit_batch": (
                "SELECT public.custombuild_storage_commit_batch("
                ":organization_id, CAST(:claims AS jsonb), :lease_token)"
            )
        }
    else:
        parameters["lease_duration_seconds"] = lease_duration_seconds
        statements = {
            "custombuild_storage_reserve_batch": (
                "SELECT public.custombuild_storage_reserve_batch("
                ":organization_id, CAST(:claims AS jsonb), :lease_token, "
                ":lease_duration_seconds)"
            ),
            "custombuild_storage_renew_batch": (
                "SELECT public.custombuild_storage_renew_batch("
                ":organization_id, CAST(:claims AS jsonb), :lease_token, "
                ":lease_duration_seconds)"
            ),
        }
    return connection.execute(text(statements[function_name]), parameters).scalar_one()


class _MissingRecoveryObject(Exception):
    def __init__(self) -> None:
        super().__init__("missing recovery object")
        self.response = {"ResponseMetadata": {"HTTPStatusCode": 404}}


class _RecoveryS3:
    def __init__(
        self,
        objects: Mapping[tuple[str, str], tuple[str, int]],
        *,
        on_delete: Callable[[str, str], None] | None = None,
    ) -> None:
        self.objects = dict(objects)
        self.on_delete = on_delete
        self.deleted: list[tuple[str, str]] = []
        self.headed: list[tuple[str, str]] = []
        self.operations: list[tuple[str, str, str]] = []

    def delete_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]:
        self.deleted.append((Bucket, Key))
        self.operations.append(("delete", Bucket, Key))
        if self.on_delete is not None:
            self.on_delete(Bucket, Key)
        self.objects.pop((Bucket, Key), None)
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]:
        self.headed.append((Bucket, Key))
        self.operations.append(("head", Bucket, Key))
        identity = self.objects.get((Bucket, Key))
        if identity is None:
            raise _MissingRecoveryObject
        sha256, size_bytes = identity
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "ContentLength": size_bytes,
            "Metadata": {"sha256": sha256},
        }


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


def _effective_table_privileges(engine: Engine) -> dict[str, frozenset[str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT object.relname, candidate.name "
                "FROM pg_class object "
                "JOIN pg_namespace namespace ON namespace.oid = object.relnamespace "
                "CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'), "
                "('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) AS candidate(name) "
                "WHERE namespace.nspname = 'public' "
                "AND object.relkind IN ('r', 'p', 'v', 'm', 'f') "
                "AND has_table_privilege(current_user, object.oid, candidate.name) "
                "ORDER BY object.relname, candidate.name"
            )
        )
        privileges: dict[str, set[str]] = {}
        for table_name, privilege in rows:
            privileges.setdefault(str(table_name), set()).add(str(privilege))
    return {table: frozenset(values) for table, values in privileges.items()}


def _effective_sequence_privileges(engine: Engine) -> list[tuple[str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT object.relname, candidate.name "
                "FROM pg_class object "
                "JOIN pg_namespace namespace ON namespace.oid = object.relnamespace "
                "CROSS JOIN (VALUES ('USAGE'), ('SELECT'), ('UPDATE')) AS candidate(name) "
                "WHERE namespace.nspname = 'public' AND object.relkind = 'S' "
                "AND has_sequence_privilege(current_user, object.oid, candidate.name) "
                "ORDER BY object.relname, candidate.name"
            )
        )
        return [(str(sequence), str(privilege)) for sequence, privilege in rows]


def _runtime_memberships(engine: Engine) -> list[tuple[str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT member_role.rolname, granted_role.rolname "
                "FROM pg_auth_members membership "
                "JOIN pg_roles member_role ON member_role.oid = membership.member "
                "JOIN pg_roles granted_role ON granted_role.oid = membership.roleid "
                "WHERE member_role.rolname IN ('custombuild_api', 'custombuild_worker') "
                "ORDER BY member_role.rolname, granted_role.rolname"
            )
        )
        return [(str(member), str(granted)) for member, granted in rows]


def _public_object_privileges(engine: Engine) -> list[tuple[str, str]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT object.relname, privilege.privilege_type "
                "FROM pg_class object "
                "JOIN pg_namespace namespace ON namespace.oid = object.relnamespace "
                "CROSS JOIN LATERAL aclexplode(object.relacl) privilege "
                "WHERE namespace.nspname = 'public' "
                "AND object.relkind IN ('r', 'p', 'S', 'v', 'm', 'f') "
                "AND privilege.grantee = 0 "
                "ORDER BY object.relname, privilege.privilege_type"
            )
        )
        return [(str(object_name), str(privilege)) for object_name, privilege in rows]


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
    bootstrap_url, _, api_url, worker_url = _urls()
    bootstrap_engine = create_engine(bootstrap_url)
    api_engine = create_engine(api_url)
    worker_engine = create_engine(worker_url)
    try:
        assert _direct_table_privileges(api_engine) == _expected(API_TABLE_PRIVILEGES)
        assert _effective_table_privileges(api_engine) == _expected(API_TABLE_PRIVILEGES)
        assert _direct_table_privileges(worker_engine) == _expected(WORKER_TABLE_PRIVILEGES)
        assert _effective_table_privileges(worker_engine) == _expected(WORKER_TABLE_PRIVILEGES)
        assert _effective_sequence_privileges(api_engine) == []
        assert _effective_sequence_privileges(worker_engine) == []
        for engine in (api_engine, worker_engine):
            with engine.connect() as connection:
                assert connection.execute(
                    text(
                        "SELECT has_schema_privilege(current_user, 'public', 'USAGE'), "
                        "has_schema_privilege(current_user, 'public', 'CREATE')"
                    )
                ).one() == (True, False)
        assert _runtime_memberships(bootstrap_engine) == []
        assert _public_object_privileges(bootstrap_engine) == []
    finally:
        bootstrap_engine.dispose()
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
            assert (
                connection.execute(
                    text("SELECT id FROM users WHERE id = :id"), {"id": user_id}
                ).scalar_one()
                == user_id
            )
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
            connection.execute(text("DROP FUNCTION IF EXISTS runtime_privilege_future_function()"))
            connection.execute(text("DROP TABLE IF EXISTS runtime_privilege_future_table"))
            connection.execute(text("DROP SEQUENCE IF EXISTS runtime_privilege_future_sequence"))
            connection.execute(text("CREATE TABLE runtime_privilege_future_table (id integer)"))
            connection.execute(text("CREATE SEQUENCE runtime_privilege_future_sequence"))
            connection.execute(
                text(
                    "CREATE FUNCTION runtime_privilege_future_function() RETURNS integer "
                    "LANGUAGE sql AS 'SELECT 1'"
                )
            )

        for engine in (api, worker):
            _assert_statement_denied(
                engine,
                "SELECT id FROM runtime_privilege_future_table",
            )
            _assert_statement_denied(
                engine,
                "SELECT nextval('runtime_privilege_future_sequence')",
            )
            _assert_statement_denied(
                engine,
                "SELECT runtime_privilege_future_function()",
            )
    finally:
        with migrator.begin() as connection:
            connection.execute(text("DROP FUNCTION IF EXISTS runtime_privilege_future_function()"))
            connection.execute(text("DROP TABLE IF EXISTS runtime_privilege_future_table"))
            connection.execute(text("DROP SEQUENCE IF EXISTS runtime_privilege_future_sequence"))
        api.dispose()
        worker.dispose()
        migrator.dispose()


@pytest.mark.postgres
def test_api_can_read_but_cannot_row_lock_immutable_release_storage_tables() -> None:
    _, _, api_url, _ = _urls()
    api = create_engine(api_url)
    try:
        with api.connect() as connection:
            transaction = connection.begin()
            for table in ("releases", "artifacts", "stored_objects"):
                connection.execute(text(f"SELECT * FROM {table} LIMIT 0"))  # noqa: S608
                with pytest.raises(DBAPIError, match="permission denied"):
                    connection.execute(
                        text(f"SELECT * FROM {table} LIMIT 1 FOR UPDATE")  # noqa: S608
                    )
                transaction.rollback()
                transaction = connection.begin()
            transaction.rollback()
    finally:
        api.dispose()


@pytest.mark.postgres
def test_capacity_attestation_is_fresh_exact_and_control_plane_only(
    storage_probe: _StorageProbe,
) -> None:
    connection = storage_probe.connection

    stale_attempt = connection.begin_nested()
    try:
        with pytest.raises(DBAPIError, match="attestation timestamp is stale"):
            _attest_current_capacity(
                connection,
                attested_at="clock_timestamp() - INTERVAL '6 minutes'",
            )
    finally:
        stale_attempt.rollback()

    _attest_current_capacity(connection)
    _set_local_role(connection, "custombuild_api")
    _set_tenant(connection, storage_probe.organization_id)
    assert (
        connection.execute(
            text("SELECT capacity_verified FROM storage_global_quotas WHERE id = 1")
        ).scalar_one()
        is True
    )


@pytest.mark.postgres
def test_reserve_is_serialized_behind_maintenance_and_current_boot_recovery(
    storage_probe: _StorageProbe,
) -> None:
    connection = storage_probe.connection
    claim = _claim(storage_probe, label="g-maintenance", size_bytes=41)
    gate_token = str(uuid.uuid4())
    _set_local_role(connection, None)
    connection.execute(
        text(
            "UPDATE storage_global_quotas SET maintenance_token = :gate_token, "
            "maintenance_epoch = maintenance_epoch + 1, "
            "maintenance_started_at = clock_timestamp(), "
            "maintenance_database_started_at = pg_postmaster_start_time(), "
            "maintenance_owner_expires_at = clock_timestamp() + INTERVAL '2 minutes', "
            "capacity_verified = false, recovery_database_started_at = NULL, "
            "recovery_completed_at = NULL WHERE id = 1"
        ),
        {"gate_token": gate_token},
    )
    _set_local_role(connection, "custombuild_api")
    _set_tenant(connection, storage_probe.organization_id)
    _assert_connection_error(
        connection,
        "SELECT public.custombuild_storage_reserve_batch("
        ":organization_id, CAST(:claims AS jsonb), :lease_token, 120)",
        {
            "organization_id": storage_probe.organization_id,
            "claims": json.dumps([claim], separators=(",", ":"), sort_keys=True),
            "lease_token": str(uuid.uuid4()),
        },
        match="STORAGE_MAINTENANCE_ACTIVE",
    )

    _set_local_role(connection, None)
    connection.execute(
        text(
            "UPDATE storage_global_quotas SET maintenance_token = NULL, "
            "maintenance_started_at = NULL, maintenance_owner_expires_at = NULL, "
            "maintenance_database_started_at = NULL "
            "WHERE id = 1"
        )
    )
    _set_local_role(connection, "custombuild_api")
    _set_tenant(connection, storage_probe.organization_id)
    _assert_connection_error(
        connection,
        "SELECT public.custombuild_storage_reserve_batch("
        ":organization_id, CAST(:claims AS jsonb), :lease_token, 120)",
        {
            "organization_id": storage_probe.organization_id,
            "claims": json.dumps([claim], separators=(",", ":"), sort_keys=True),
            "lease_token": str(uuid.uuid4()),
        },
        match="STORAGE_RECOVERY_REQUIRED",
    )

    _set_local_role(connection, None)
    connection.execute(
        text(
            "UPDATE storage_global_quotas SET "
            "recovery_database_started_at = pg_postmaster_start_time(), "
            "recovery_completed_at = clock_timestamp() WHERE id = 1"
        )
    )
    _attest_current_capacity(connection)
    _set_local_role(connection, "custombuild_api")
    _set_tenant(connection, storage_probe.organization_id)
    reserved = _call_storage_batch(
        connection,
        "custombuild_storage_reserve_batch",
        storage_probe.organization_id,
        [claim],
        str(uuid.uuid4()),
        120,
    )
    assert isinstance(reserved, dict)
    assert reserved["newly_reserved_count"] == 1

    # A valid attestation cannot be replayed forever: the SECURITY DEFINER
    # reserve entry point checks the database clock rather than caller time.
    _set_local_role(connection, None)
    connection.execute(
        text(
            "UPDATE storage_global_quotas "
            "SET capacity_verified_at = clock_timestamp() - INTERVAL '11 minutes' "
            "WHERE id = 1"
        )
    )
    stale_claim = _claim(storage_probe, label="d-stale-clock", size_bytes=17)
    _set_local_role(connection, "custombuild_api")
    _set_tenant(connection, storage_probe.organization_id)
    _assert_connection_error(
        connection,
        "SELECT public.custombuild_storage_reserve_batch("
        ":organization_id, CAST(:claims AS jsonb), :lease_token, 120)",
        {
            "organization_id": storage_probe.organization_id,
            "claims": json.dumps([stale_claim], separators=(",", ":"), sort_keys=True),
            "lease_token": str(uuid.uuid4()),
        },
        match="STORAGE_CAPACITY_UNVERIFIED",
    )
    _attest_current_capacity(connection)

    for role in ("custombuild_api", "custombuild_worker"):
        _set_local_role(connection, role)
        _set_tenant(connection, storage_probe.organization_id)
        _assert_connection_error(
            connection,
            "UPDATE storage_global_quotas SET capacity_verified = false WHERE id = 1",
            match="permission denied",
        )
        _assert_connection_error(
            connection,
            "SELECT public.custombuild_storage_invalidate_capacity(:evidence_hash)",
            {"evidence_hash": "f" * 64},
            match="permission denied",
        )
        _assert_connection_error(
            connection,
            "SELECT public._custombuild_storage_require_tenant(:organization_id)",
            {"organization_id": storage_probe.organization_id},
            match="permission denied",
        )

    _set_local_role(connection, None)
    assert (
        connection.execute(
            text("SELECT capacity_verified FROM storage_global_quotas WHERE id = 1")
        ).scalar_one()
        is True
    )


@pytest.mark.postgres
def test_security_definer_storage_lifecycle_allows_trusted_counter_mutations(
    storage_probe: _StorageProbe,
) -> None:
    connection = storage_probe.connection
    _set_local_role(connection, None)
    baseline = _global_storage_counters(connection)
    _attest_current_capacity(connection)

    first = _claim(storage_probe, label="a-reserved", size_bytes=101)
    second = _claim(storage_probe, label="b-reserved", size_bytes=202)
    third = _claim(storage_probe, label="c-after-attestation", size_bytes=303)
    lease_token = str(uuid.uuid4())
    _set_local_role(connection, "custombuild_api")
    _set_tenant(connection, storage_probe.organization_id)
    reservation = _call_storage_batch(
        connection,
        "custombuild_storage_reserve_batch",
        storage_probe.organization_id,
        [first, second],
        lease_token,
        120,
    )
    assert isinstance(reservation, dict)
    assert reservation["newly_reserved_bytes"] == 303
    assert reservation["newly_reserved_count"] == 2
    assert {item["object_key"] for item in reservation["objects"]} == {
        first["object_key"],
        second["object_key"],
    }
    before_renewal = dict(
        connection.execute(
            text(
                "SELECT object_key, lease_expires_at FROM stored_objects "
                "WHERE organization_id = :organization_id ORDER BY object_key"
            ),
            {"organization_id": storage_probe.organization_id},
        ).all()
    )
    _call_storage_batch(
        connection,
        "custombuild_storage_renew_batch",
        storage_probe.organization_id,
        [first, second],
        lease_token,
        600,
    )
    after_renewal = dict(
        connection.execute(
            text(
                "SELECT object_key, lease_expires_at FROM stored_objects "
                "WHERE organization_id = :organization_id ORDER BY object_key"
            ),
            {"organization_id": storage_probe.organization_id},
        ).all()
    )
    assert all(after_renewal[key] > expiry for key, expiry in before_renewal.items())

    wrong_lease = str(uuid.uuid4())
    _assert_connection_error(
        connection,
        "SELECT public.custombuild_storage_commit_batch("
        ":organization_id, CAST(:claims AS jsonb), :lease_token)",
        {
            "organization_id": storage_probe.organization_id,
            "claims": json.dumps([first], separators=(",", ":"), sort_keys=True),
            "lease_token": wrong_lease,
        },
        match="reserved object is owned by another lease",
    )
    _call_storage_batch(
        connection,
        "custombuild_storage_commit_batch",
        storage_probe.organization_id,
        [first],
        lease_token,
    )
    # The same immutable identity can be committed again without double counting.
    _call_storage_batch(
        connection,
        "custombuild_storage_commit_batch",
        storage_probe.organization_id,
        [first],
        lease_token,
    )

    committed_bytes, committed_count, reserved_bytes, reserved_count = baseline
    _set_local_role(connection, None)
    assert _global_storage_counters(connection) == (
        committed_bytes + 101,
        committed_count + 1,
        reserved_bytes + 202,
        reserved_count + 1,
    )

    # The attestation baseline remains internally exact, while a successful
    # SECURITY DEFINER commit is itself a trusted ledger mutation. A second
    # sequential reservation must not stall until the next five-minute attest.
    _set_local_role(connection, "custombuild_api")
    _set_tenant(connection, storage_probe.organization_id)
    resumed = _call_storage_batch(
        connection,
        "custombuild_storage_reserve_batch",
        storage_probe.organization_id,
        [third],
        lease_token,
        120,
    )
    assert isinstance(resumed, dict)
    assert resumed["newly_reserved_count"] == 1

    tenant_mismatch = connection.begin_nested()
    try:
        _set_tenant(connection, str(uuid.uuid4()))
        with pytest.raises(DBAPIError, match="STORAGE_TENANT_CONTEXT_MISMATCH"):
            _call_storage_batch(
                connection,
                "custombuild_storage_renew_batch",
                storage_probe.organization_id,
                [third],
                lease_token,
                120,
            )
    finally:
        tenant_mismatch.rollback()

    _assert_connection_error(
        connection,
        "UPDATE stored_objects SET size_bytes = 1 "
        "WHERE organization_id = :organization_id AND object_key = :object_key",
        {
            "organization_id": storage_probe.organization_id,
            "object_key": first["object_key"],
        },
        match="permission denied",
    )


@pytest.mark.postgres
def test_worker_reaper_is_token_bound_and_preserves_exact_accounting(
    storage_probe: _StorageProbe,
) -> None:
    connection = storage_probe.connection
    _set_local_role(connection, None)
    baseline = _global_storage_counters(connection)
    _attest_current_capacity(connection)

    expired = _claim(storage_probe, label="e-expired", size_bytes=404)
    expired_lease = str(uuid.uuid4())
    _set_local_role(connection, "custombuild_api")
    _set_tenant(connection, storage_probe.organization_id)
    _call_storage_batch(
        connection,
        "custombuild_storage_reserve_batch",
        storage_probe.organization_id,
        [expired],
        expired_lease,
        120,
    )
    _assert_connection_error(
        connection,
        "SELECT public.custombuild_storage_claim_expired_reservations("
        ":organization_id, :claim_token, 120, 10)",
        {
            "organization_id": storage_probe.organization_id,
            "claim_token": str(uuid.uuid5(uuid.NAMESPACE_URL, "api-reaper-denied")),
        },
        match="permission denied",
    )

    _set_local_role(connection, None)
    connection.execute(
        text(
            "UPDATE stored_objects SET lease_expires_at = clock_timestamp() - INTERVAL '1 second' "
            "WHERE organization_id = :organization_id AND object_key = :object_key"
        ),
        {
            "organization_id": storage_probe.organization_id,
            "object_key": expired["object_key"],
        },
    )
    expired_claim_seed = str(uuid.uuid5(uuid.NAMESPACE_URL, "expired-reaper-seed"))
    _set_local_role(connection, "custombuild_worker")
    _set_tenant(connection, storage_probe.organization_id)
    expired_reaped = connection.execute(
        text(
            "SELECT public.custombuild_storage_claim_expired_reservations("
            ":organization_id, :claim_token, 120, 10)"
        ),
        {
            "organization_id": storage_probe.organization_id,
            "claim_token": expired_claim_seed,
        },
    ).scalar_one()
    assert isinstance(expired_reaped, list)
    assert len(expired_reaped) == 1
    expired_claim_token = expired_reaped[0]["claim_token"]
    assert expired_claim_token != expired_claim_seed
    assert expired_claim_token[14] == "4"

    _assert_connection_error(
        connection,
        "SELECT public.custombuild_storage_finalize_reap("
        ":organization_id, :object_key, :sha256, :size_bytes, :claim_token, "
        ":capacity_bucket)",
        {
            "organization_id": storage_probe.organization_id,
            "object_key": expired["object_key"],
            "sha256": expired["sha256"],
            "size_bytes": expired["size_bytes"],
            "claim_token": str(uuid.uuid4()),
            "capacity_bucket": "custombuild-artifacts",
        },
        match="reaper ownership or identity was lost",
    )
    # A capacity re-attestation may race provider I/O. Finalization must bind
    # the same bucket again while holding the global row lock and retain both
    # the ledger row and full debit when it changed after preflight.
    _set_local_role(connection, None)
    connection.execute(
        text(
            "UPDATE storage_global_quotas SET capacity_bucket = 'replacement-artifacts' "
            "WHERE id = 1"
        )
    )
    _set_local_role(connection, "custombuild_worker")
    _set_tenant(connection, storage_probe.organization_id)
    _assert_connection_error(
        connection,
        "SELECT public.custombuild_storage_finalize_reap("
        ":organization_id, :object_key, :sha256, :size_bytes, :claim_token, "
        ":capacity_bucket)",
        {
            "organization_id": storage_probe.organization_id,
            "object_key": expired["object_key"],
            "sha256": expired["sha256"],
            "size_bytes": expired["size_bytes"],
            "claim_token": expired_claim_token,
            "capacity_bucket": "custombuild-artifacts",
        },
        match="STORAGE_BUCKET_MISMATCH",
    )
    _set_local_role(connection, None)
    assert connection.execute(
        text(
            "SELECT state, claim_token FROM stored_objects "
            "WHERE organization_id = :organization_id AND object_key = :object_key"
        ),
        {
            "organization_id": storage_probe.organization_id,
            "object_key": expired["object_key"],
        },
    ).one() == ("reaping", expired_claim_token)
    assert _global_storage_counters(connection) == (
        baseline[0],
        baseline[1],
        baseline[2] + 404,
        baseline[3] + 1,
    )
    connection.execute(
        text(
            "UPDATE storage_global_quotas SET capacity_bucket = 'custombuild-artifacts' "
            "WHERE id = 1"
        )
    )
    _set_local_role(connection, "custombuild_worker")
    _set_tenant(connection, storage_probe.organization_id)
    assert (
        connection.execute(
            text(
                "SELECT public.custombuild_storage_finalize_reap("
                ":organization_id, :object_key, :sha256, :size_bytes, :claim_token, "
                ":capacity_bucket)"
            ),
            {
                "organization_id": storage_probe.organization_id,
                "object_key": expired["object_key"],
                "sha256": expired["sha256"],
                "size_bytes": expired["size_bytes"],
                "claim_token": expired_claim_token,
                "capacity_bucket": "custombuild-artifacts",
            },
        ).scalar_one()
        is True
    )

    committed = _claim(storage_probe, label="f-delete-pending", size_bytes=505)
    committed_lease = str(uuid.uuid4())
    _set_local_role(connection, "custombuild_api")
    _set_tenant(connection, storage_probe.organization_id)
    _call_storage_batch(
        connection,
        "custombuild_storage_reserve_batch",
        storage_probe.organization_id,
        [committed],
        committed_lease,
        120,
    )
    _call_storage_batch(
        connection,
        "custombuild_storage_commit_batch",
        storage_probe.organization_id,
        [committed],
        committed_lease,
    )
    _set_local_role(connection, None)
    connection.execute(
        text(
            "UPDATE stored_objects SET state = 'delete_pending' "
            "WHERE organization_id = :organization_id AND object_key = :object_key"
        ),
        {
            "organization_id": storage_probe.organization_id,
            "object_key": committed["object_key"],
        },
    )
    delete_claim_seed = str(uuid.uuid4())
    _set_local_role(connection, "custombuild_worker")
    _set_tenant(connection, storage_probe.organization_id)
    delete_reaped = connection.execute(
        text(
            "SELECT public.custombuild_storage_claim_delete_pending("
            ":organization_id, :claim_token, 120, 10)"
        ),
        {
            "organization_id": storage_probe.organization_id,
            "claim_token": delete_claim_seed,
        },
    ).scalar_one()
    assert isinstance(delete_reaped, list)
    assert len(delete_reaped) == 1
    delete_claim_token = delete_reaped[0]["claim_token"]
    assert delete_claim_token != delete_claim_seed
    assert delete_claim_token[14] == "5"
    assert (
        connection.execute(
            text(
                "SELECT public.custombuild_storage_finalize_reap("
                ":organization_id, :object_key, :sha256, :size_bytes, :claim_token, "
                ":capacity_bucket)"
            ),
            {
                "organization_id": storage_probe.organization_id,
                "object_key": committed["object_key"],
                "sha256": committed["sha256"],
                "size_bytes": committed["size_bytes"],
                "claim_token": delete_claim_token,
                "capacity_bucket": "custombuild-artifacts",
            },
        ).scalar_one()
        is True
    )

    _assert_connection_error(
        connection,
        "UPDATE storage_tenant_quotas SET committed_bytes = 0 "
        "WHERE organization_id = :organization_id",
        {"organization_id": storage_probe.organization_id},
        match="permission denied",
    )
    _set_local_role(connection, None)
    assert _global_storage_counters(connection) == baseline
    assert (
        connection.execute(
            text("SELECT count(*) FROM stored_objects WHERE organization_id = :organization_id"),
            {"organization_id": storage_probe.organization_id},
        ).scalar_one()
        == 0
    )


@pytest.mark.postgres
def test_cold_start_recovery_reaps_expired_staging_and_rejects_racing_reference() -> None:
    bootstrap_url, migrator_url, api_url, worker_url = _urls()
    suffix = uuid.uuid4().hex[:12]
    organization_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    design_version_id = str(uuid.uuid4())
    generation_job_id = str(uuid.uuid4())
    domain_reference_ids = {
        "imported_assets": str(uuid.uuid4()),
        "external_evidence": str(uuid.uuid4()),
        "artifacts": str(uuid.uuid4()),
    }
    lease_token = str(uuid.uuid4())
    bucket = "custombuild-recovery-integration"
    object_key = f"integration/{organization_id}/expired-generation.bin"
    sha256 = "a" * 64
    size_bytes = 137
    domain_storage_identities: dict[str, dict[str, str | int]] = {
        "imported_assets": {
            "object_key": f"integration/{organization_id}/expired-import.bin",
            "sha256": "1" * 64,
            "size_bytes": 139,
            "owner_type": "imported_asset",
            "owner_id": domain_reference_ids["imported_assets"],
            "idempotency_key": f"imported:{domain_reference_ids['imported_assets']}",
        },
        "external_evidence": {
            "object_key": f"integration/{organization_id}/expired-evidence.bin",
            "sha256": "2" * 64,
            "size_bytes": 149,
            "owner_type": "external_evidence",
            "owner_id": domain_reference_ids["external_evidence"],
            "idempotency_key": (f"external-evidence:{domain_reference_ids['external_evidence']}"),
        },
        "artifacts": {
            "object_key": object_key,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "owner_type": "generation_job",
            "owner_id": generation_job_id,
            "idempotency_key": (
                f"generation:{generation_job_id}:expired:{domain_reference_ids['artifacts']}"
            ),
        },
    }
    race_reserved_bytes = sum(
        int(identity["size_bytes"]) for identity in domain_storage_identities.values()
    )
    race_reserved_count = len(domain_storage_identities)
    ordered_object_key = f"integration/{organization_id}/ordered-reference.bin"
    ordered_sha256 = "9" * 64
    ordered_size_bytes = 173
    ordered_asset_id = str(uuid.uuid4())
    missing_object_key = f"integration/{organization_id}/missing-quota.bin"
    missing_sha256 = "b" * 64
    missing_size_bytes = 211

    bootstrap = create_engine(bootstrap_url)
    migrator = create_engine(migrator_url)
    api = create_engine(
        api_url,
        connect_args={"options": "-c statement_timeout=10000"},
    )
    worker = create_engine(
        worker_url,
        connect_args={"options": "-c statement_timeout=10000"},
    )
    store = PostgresRecoveryStore(migrator)
    global_before: dict[str, object] | None = None
    delete_races_started: set[str] = set()
    domain_threads: list[threading.Thread] = []
    domain_executed: set[str] = set()
    domain_errors: dict[str, Exception] = {}
    domain_finished_during_delete: dict[str, bool] = {}
    generation_threads: list[threading.Thread] = []
    generation_attempted: set[str] = set()
    generation_executed: set[str] = set()
    generation_errors: dict[str, Exception] = {}
    generation_preflight_delays: dict[str, int] = {}
    generation_finished_during_delete: dict[str, bool] = {}
    wrong_bucket_preflight_count = 0
    job_wins_ready = threading.Event()
    release_job_winner = threading.Event()
    job_wins_errors: list[Exception] = []
    job_wins_thread: threading.Thread | None = None

    def hold_job_transition_lock() -> None:
        try:
            with worker.begin() as connection:
                _set_tenant(connection, organization_id)
                updated = connection.execute(
                    text(
                        "UPDATE generation_jobs SET status = 'queued', error = NULL, "
                        "finished_at = NULL, updated_at = clock_timestamp() "
                        "WHERE organization_id = :organization_id "
                        "AND id = :generation_job_id"
                    ),
                    {
                        "organization_id": organization_id,
                        "generation_job_id": generation_job_id,
                    },
                )
                assert updated.rowcount == 1
                job_wins_ready.set()
                assert release_job_winner.wait(timeout=10)
        except Exception as exc:  # noqa: BLE001 - transferred back from the test thread
            job_wins_errors.append(exc)
            job_wins_ready.set()

    def insert_domain_reference(reference_table: str) -> None:
        storage_identity = domain_storage_identities[reference_table]
        statements = {
            "imported_assets": (
                "INSERT INTO imported_assets ("
                "id, organization_id, project_id, sha256, object_key, size_bytes, "
                "media_type, original_filename, created_by, created_at, updated_at"
                ") VALUES ("
                ":id, :organization_id, :project_id, :sha256, :object_key, "
                ":size_bytes, :media_type, 'late-reference.bin', :created_by, "
                "clock_timestamp(), clock_timestamp())"
            ),
            "external_evidence": (
                "INSERT INTO external_evidence ("
                "id, organization_id, project_id, evidence_type, rule_id, catalog_id, "
                "catalog_version, design_hash, object_key, sha256, size_bytes, content_type, "
                "created_by, expires_at, revoked_at, created_at, updated_at"
                ") VALUES ("
                ":id, :organization_id, :project_id, 'certificate', 'recovery-rule', "
                "'recovery-catalog', '1', :design_hash, :object_key, :sha256, "
                ":size_bytes, :media_type, :created_by, NULL, NULL, "
                "clock_timestamp(), clock_timestamp())"
            ),
            "artifacts": (
                "INSERT INTO artifacts ("
                "id, organization_id, generation_job_id, kind, object_key, sha256, "
                "size_bytes, content_type, created_at, updated_at"
                ") VALUES ("
                ":id, :organization_id, :generation_job_id, 'expired', :object_key, "
                ":sha256, :size_bytes, :media_type, clock_timestamp(), clock_timestamp())"
            ),
        }
        runtime = worker if reference_table == "artifacts" else api
        try:
            with runtime.begin() as connection:
                _set_tenant(connection, organization_id)
                inserted = connection.execute(
                    text(statements[reference_table]),
                    {
                        "id": domain_reference_ids[reference_table],
                        "organization_id": organization_id,
                        "project_id": project_id,
                        "generation_job_id": generation_job_id,
                        "sha256": storage_identity["sha256"],
                        "object_key": storage_identity["object_key"],
                        "size_bytes": storage_identity["size_bytes"],
                        "media_type": "application/octet-stream",
                        "created_by": user_id,
                        "design_hash": "8" * 64,
                    },
                )
                assert inserted.rowcount == 1
                domain_executed.add(reference_table)
        except Exception as exc:  # noqa: BLE001 - transferred back from the test thread
            domain_errors[reference_table] = exc

    def mutate_generation_liveness(label: str) -> None:
        statements = {
            "failed_to_queued": (
                "UPDATE generation_jobs SET status = 'queued', lease_token = NULL, "
                "lease_expires_at = NULL, finished_at = NULL, updated_at = clock_timestamp() "
                "WHERE organization_id = :organization_id AND id = :generation_job_id"
            ),
            "renew_live_lease": (
                "UPDATE generation_jobs SET lease_token = :lease_token, "
                "lease_expires_at = clock_timestamp() + INTERVAL '2 minutes', "
                "updated_at = clock_timestamp() "
                "WHERE organization_id = :organization_id AND id = :generation_job_id"
            ),
        }
        try:
            with api.begin() as connection:
                _set_tenant(connection, organization_id)
                retry_after = connection.execute(
                    text(
                        "SELECT public.custombuild_storage_prepare_generation_retry("
                        ":organization_id, :generation_job_id)"
                    ),
                    {
                        "organization_id": organization_id,
                        "generation_job_id": generation_job_id,
                    },
                ).scalar_one()
                assert isinstance(retry_after, int)
                assert 1 <= retry_after <= 3605
                generation_preflight_delays[label] = retry_after
            with worker.begin() as connection:
                _set_tenant(connection, organization_id)
                generation_attempted.add(label)
                updated = connection.execute(
                    text(statements[label]),
                    {
                        "organization_id": organization_id,
                        "generation_job_id": generation_job_id,
                        "lease_token": str(uuid.uuid4()),
                    },
                )
                assert updated.rowcount == 1
                generation_executed.add(label)
        except Exception as exc:  # noqa: BLE001 - transferred back from the test thread
            generation_errors[label] = exc

    def start_reference_insert(_bucket: str, _object_key: str) -> None:
        reference_table = next(
            (
                table
                for table, identity in domain_storage_identities.items()
                if identity["object_key"] == _object_key
            ),
            None,
        )
        assert reference_table is not None
        if reference_table in delete_races_started:
            return
        delete_races_started.add(reference_table)
        thread = threading.Thread(
            target=insert_domain_reference,
            args=(reference_table,),
            name=f"storage-recovery-domain-{reference_table}",
            daemon=True,
        )
        domain_threads.append(thread)
        thread.start()
        thread.join(timeout=5)
        domain_finished_during_delete[reference_table] = not thread.is_alive()
        if reference_table != "artifacts":
            return
        for label in ("failed_to_queued", "renew_live_lease"):
            thread = threading.Thread(
                target=mutate_generation_liveness,
                args=(label,),
                name=f"storage-recovery-generation-{label}",
                daemon=True,
            )
            generation_threads.append(thread)
            thread.start()
            thread.join(timeout=5)
            generation_finished_during_delete[label] = not thread.is_alive()

    def reap_with_wrong_bucket_preflight(
        session_factory_: sessionmaker[Session],
        s3_client: StorageReaperS3Client,
        active_bucket: str,
        claim: StorageReapClaim,
    ) -> StorageReapResult:
        nonlocal wrong_bucket_preflight_count
        wrong_bucket_s3 = _RecoveryS3(
            {(bucket, claim.object_key): (claim.sha256, claim.size_bytes)}
        )
        with pytest.raises(
            StorageReaperInvariantError,
            match="storage provider bucket does not match the attested ledger bucket",
        ):
            reap_storage_claim(
                session_factory_,
                wrong_bucket_s3,
                "custombuild-recovery-wrong-bucket",
                claim,
            )
        assert wrong_bucket_s3.operations == []
        wrong_bucket_preflight_count += 1
        return reap_storage_claim(
            session_factory_,
            s3_client,
            active_bucket,
            claim,
        )

    try:
        with bootstrap.begin() as connection:
            global_before = dict(
                connection.execute(
                    text("SELECT * FROM storage_global_quotas WHERE id = 1 FOR UPDATE")
                )
                .mappings()
                .one()
            )
            assert global_before["reserved_bytes"] == 0
            assert global_before["reserved_count"] == 0
            assert global_before["maintenance_token"] is None

            connection.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, created_at, updated_at) "
                    "VALUES (:id, 'Recovery integration tenant', :slug, "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {"id": organization_id, "slug": f"recovery-integration-{suffix}"},
            )
            connection.execute(
                text(
                    "INSERT INTO users (id, oidc_sub, email, name, created_at, updated_at) "
                    "VALUES (:id, :subject, :email, 'Recovery integration user', "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {
                    "id": user_id,
                    "subject": f"recovery-integration-{suffix}",
                    "email": f"recovery-integration-{suffix}@example.test",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO projects ("
                    "id, organization_id, name, description, furniture_type, "
                    "current_revision, draft_revision, archived, created_at, updated_at"
                    ") VALUES ("
                    ":id, :organization_id, 'Recovery integration project', '', "
                    "'bookcase', 0, 0, false, clock_timestamp(), clock_timestamp())"
                ),
                {"id": project_id, "organization_id": organization_id},
            )
            connection.execute(
                text(
                    "INSERT INTO design_versions ("
                    "id, organization_id, project_id, revision, status, design_hash, "
                    "context_hash, spec_json, source_provenance_json, result_json, "
                    "engine_version, template_version, template_id, "
                    "template_capability_fingerprint, rule_version, created_by, immutable, "
                    "created_at, updated_at"
                    ") VALUES ("
                    ":id, :organization_id, :project_id, 1, 'draft', :design_hash, "
                    ":context_hash, CAST(:empty_json AS json), CAST(:empty_json AS json), "
                    "CAST(:empty_json AS json), 'integration-engine', '1', 'shelving', "
                    ":template_fingerprint, 'integration-rules', :created_by, false, "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {
                    "id": design_version_id,
                    "organization_id": organization_id,
                    "project_id": project_id,
                    "design_hash": "c" * 64,
                    "context_hash": "d" * 64,
                    "empty_json": "{}",
                    "template_fingerprint": "e" * 64,
                    "created_by": user_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO generation_jobs ("
                    "id, organization_id, design_version_id, status, idempotency_key, "
                    "production_context_hash, production_engine_context_json, request_json, "
                    "result_json, attempts, lease_token, lease_expires_at, deadline_at, error, "
                    "started_at, finished_at, created_at, updated_at"
                    ") VALUES ("
                    ":id, :organization_id, :design_version_id, 'failed', "
                    ":idempotency_key, :context_hash, CAST(:empty_json AS json), "
                    "CAST(:empty_json AS json), NULL, 0, NULL, NULL, NULL, NULL, NULL, NULL, "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {
                    "id": generation_job_id,
                    "organization_id": organization_id,
                    "design_version_id": design_version_id,
                    "idempotency_key": f"recovery-{suffix}",
                    "context_hash": "f" * 64,
                    "empty_json": "{}",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO storage_tenant_quotas ("
                    "organization_id, byte_limit, object_limit, reserved_bytes, "
                    "committed_bytes, reserved_count, committed_count, created_at, updated_at"
                    ") VALUES ("
                    ":organization_id, 1000000, 1000, :size_bytes, 0, 1, 0, "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {"organization_id": organization_id, "size_bytes": size_bytes},
            )
            connection.execute(
                text(
                    "INSERT INTO stored_objects ("
                    "organization_id, object_key, project_id, sha256, size_bytes, media_type, "
                    "owner_type, owner_id, idempotency_key, state, lease_token, "
                    "lease_expires_at, claim_token, claim_expires_at, created_at, updated_at"
                    ") VALUES ("
                    ":organization_id, :object_key, :project_id, :sha256, :size_bytes, "
                    "'application/octet-stream', 'generation_job', :owner_id, "
                    ":idempotency_key, 'reserved', :lease_token, "
                    "clock_timestamp() - INTERVAL '1 minute', NULL, NULL, "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {
                    "organization_id": organization_id,
                    "object_key": object_key,
                    "project_id": project_id,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                    "owner_id": generation_job_id,
                    "idempotency_key": (
                        f"generation:{generation_job_id}:expired:"
                        f"{domain_reference_ids['artifacts']}"
                    ),
                    "lease_token": lease_token,
                },
            )
            connection.execute(
                text(
                    "UPDATE storage_global_quotas SET reserved_bytes = :size_bytes, "
                    "reserved_count = 1, capacity_verified = false, "
                    "capacity_bucket = :bucket, maintenance_token = NULL, "
                    "maintenance_started_at = NULL, maintenance_owner_expires_at = NULL, "
                    "maintenance_database_started_at = NULL, "
                    "recovery_database_started_at = NULL, recovery_completed_at = NULL, "
                    "updated_at = clock_timestamp() WHERE id = 1"
                ),
                {"size_bytes": size_bytes, "bucket": bucket},
            )

        # A crashed owner from a previous PostgreSQL boot may retain a future
        # wall-clock lease in the durable row. Recovery must take it over
        # immediately because that process cannot still exist. Conversely, a
        # second owner in this boot remains blocked until the lease expires.
        abandoned_token = str(uuid.uuid4())
        with bootstrap.begin() as connection:
            connection.execute(
                text(
                    "UPDATE storage_global_quotas SET "
                    "maintenance_token = :abandoned_token, "
                    "maintenance_epoch = maintenance_epoch + 1, "
                    "maintenance_started_at = clock_timestamp(), "
                    "maintenance_owner_expires_at = "
                    "clock_timestamp() + INTERVAL '2 minutes', "
                    "maintenance_database_started_at = "
                    "pg_postmaster_start_time() - INTERVAL '1 second', "
                    "capacity_verified = false, "
                    "recovery_database_started_at = NULL, "
                    "recovery_completed_at = NULL, updated_at = clock_timestamp() "
                    "WHERE id = 1"
                ),
                {"abandoned_token": abandoned_token},
            )
        takeover_token = str(uuid.uuid4())
        takeover_epoch = store.begin(takeover_token, bucket)
        with bootstrap.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT maintenance_token, maintenance_epoch, "
                    "maintenance_database_started_at = pg_postmaster_start_time() "
                    "FROM storage_global_quotas WHERE id = 1"
                )
            ).one() == (takeover_token, takeover_epoch, True)
        with pytest.raises(
            StorageRecoveryError,
            match="another storage recovery owns maintenance",
        ):
            store.begin(str(uuid.uuid4()), bucket)
        with bootstrap.begin() as connection:
            connection.execute(
                text(
                    "UPDATE storage_global_quotas SET maintenance_token = NULL, "
                    "maintenance_started_at = NULL, "
                    "maintenance_owner_expires_at = NULL, "
                    "maintenance_database_started_at = NULL "
                    "WHERE id = 1"
                )
            )

        # Job-transition-wins ordering: the immediate BEFORE trigger holds the
        # reserved ledger row in KEY SHARE until commit. The competing reaper
        # still sees the old failed job through MVCC, but FOR UPDATE SKIP LOCKED
        # must skip the fenced object instead of converting it to reaping.
        job_wins_thread = threading.Thread(
            target=hold_job_transition_lock,
            name="storage-recovery-job-wins",
            daemon=True,
        )
        job_wins_thread.start()
        assert job_wins_ready.wait(timeout=5)
        assert job_wins_errors == []
        with worker.begin() as connection:
            _set_tenant(connection, organization_id)
            skipped_claims = connection.execute(
                text(
                    "SELECT public.custombuild_storage_claim_expired_reservations("
                    ":organization_id, :claim_token, 120, 1)"
                ),
                {
                    "organization_id": organization_id,
                    "claim_token": str(uuid.uuid4()),
                },
            ).scalar_one()
        assert skipped_claims == []
        release_job_winner.set()
        job_wins_thread.join(timeout=10)
        assert not job_wins_thread.is_alive()
        assert job_wins_errors == []
        with bootstrap.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT generation_job.status, stored.state "
                    "FROM generation_jobs AS generation_job "
                    "JOIN stored_objects AS stored "
                    "ON stored.organization_id = generation_job.organization_id "
                    "AND stored.owner_id = generation_job.id "
                    "WHERE generation_job.id = :generation_job_id "
                    "AND stored.object_key = :object_key"
                ),
                {
                    "generation_job_id": generation_job_id,
                    "object_key": object_key,
                },
            ).one() == ("queued", "reserved")

        # Add distinct, exact ledger identities for the imported-asset and
        # external-evidence races. All three reference transactions below are
        # therefore valid except for the concurrent reaping lifecycle state;
        # an owner/idempotency mismatch cannot produce a false-positive race.
        with bootstrap.begin() as connection:
            for reference_table in ("imported_assets", "external_evidence"):
                identity = domain_storage_identities[reference_table]
                connection.execute(
                    text(
                        "INSERT INTO stored_objects ("
                        "organization_id, object_key, project_id, sha256, size_bytes, "
                        "media_type, owner_type, owner_id, idempotency_key, state, "
                        "lease_token, lease_expires_at, claim_token, claim_expires_at, "
                        "created_at, updated_at) VALUES ("
                        ":organization_id, :object_key, :project_id, :sha256, "
                        ":size_bytes, 'application/octet-stream', :owner_type, "
                        ":owner_id, :idempotency_key, 'reserved', :lease_token, "
                        "clock_timestamp() - INTERVAL '1 minute', NULL, NULL, "
                        "clock_timestamp(), clock_timestamp())"
                    ),
                    {
                        "organization_id": organization_id,
                        "project_id": project_id,
                        "object_key": identity["object_key"],
                        "sha256": identity["sha256"],
                        "size_bytes": identity["size_bytes"],
                        "owner_type": identity["owner_type"],
                        "owner_id": identity["owner_id"],
                        "idempotency_key": identity["idempotency_key"],
                        "lease_token": str(uuid.uuid4()),
                    },
                )
            connection.execute(
                text(
                    "UPDATE storage_tenant_quotas SET reserved_bytes = :reserved_bytes, "
                    "reserved_count = :reserved_count, updated_at = clock_timestamp() "
                    "WHERE organization_id = :organization_id"
                ),
                {
                    "organization_id": organization_id,
                    "reserved_bytes": race_reserved_bytes,
                    "reserved_count": race_reserved_count,
                },
            )
            connection.execute(
                text(
                    "UPDATE storage_global_quotas SET "
                    "reserved_bytes = :reserved_bytes, "
                    "reserved_count = :reserved_count, updated_at = clock_timestamp() "
                    "WHERE id = 1"
                ),
                {
                    "reserved_bytes": race_reserved_bytes,
                    "reserved_count": race_reserved_count,
                },
            )
        with bootstrap.connect() as connection:
            stored_race_identities = {
                row.object_key: (
                    row.owner_type,
                    str(row.owner_id),
                    row.idempotency_key,
                    row.sha256,
                    int(row.size_bytes),
                    row.media_type,
                )
                for row in connection.execute(
                    text(
                        "SELECT object_key, owner_type, owner_id, idempotency_key, "
                        "sha256, size_bytes, media_type FROM stored_objects "
                        "WHERE organization_id = :organization_id"
                    ),
                    {"organization_id": organization_id},
                )
            }
        assert stored_race_identities == {
            str(identity["object_key"]): (
                identity["owner_type"],
                str(identity["owner_id"]),
                identity["idempotency_key"],
                identity["sha256"],
                int(identity["size_bytes"]),
                "application/octet-stream",
            )
            for identity in domain_storage_identities.values()
        }

        wrong_token = str(uuid.uuid4())
        with pytest.raises(
            StorageRecoveryError,
            match="configured S3 bucket does not match the storage ledger",
        ):
            store.begin(wrong_token, "custombuild-recovery-wrong-bucket")
        with bootstrap.connect() as connection:
            mismatch_state = connection.execute(
                text(
                    "SELECT maintenance_token, reserved_count FROM storage_global_quotas "
                    "WHERE id = 1"
                )
            ).one()
            assert mismatch_state == (None, race_reserved_count)
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM stored_objects "
                        "WHERE organization_id = :organization_id "
                        "AND state = 'reserved'"
                    ),
                    {"organization_id": organization_id},
                ).scalar_one()
                == race_reserved_count
            )

        s3 = _RecoveryS3(
            {
                (bucket, str(identity["object_key"])): (
                    str(identity["sha256"]),
                    int(identity["size_bytes"]),
                )
                for identity in domain_storage_identities.values()
            },
            on_delete=start_reference_insert,
        )
        recover_storage(
            store,
            s3,
            bucket,
            timeout_seconds=30,
            poll_seconds=0.01,
            reap_claim=reap_with_wrong_bucket_preflight,
        )
        assert delete_races_started == set(_DOMAIN_REFERENCE_TABLES)
        assert domain_executed == set(_DOMAIN_REFERENCE_TABLES)
        assert domain_finished_during_delete == {
            reference_table: True for reference_table in _DOMAIN_REFERENCE_TABLES
        }
        assert set(domain_errors) == set(_DOMAIN_REFERENCE_TABLES)
        assert all(isinstance(error, DBAPIError) for error in domain_errors.values())
        assert all(
            "STORAGE_DOMAIN_REFERENCE_INVALID" in str(error) for error in domain_errors.values()
        )
        assert generation_attempted == {"failed_to_queued", "renew_live_lease"}
        # The immediate BEFORE trigger rejects the UPDATE statement itself;
        # neither unsafe transition reaches transaction commit.
        assert generation_executed == set()
        assert generation_finished_during_delete == {
            "failed_to_queued": True,
            "renew_live_lease": True,
        }
        assert set(generation_errors) == {"failed_to_queued", "renew_live_lease"}
        assert all(isinstance(error, DBAPIError) for error in generation_errors.values())
        assert set(generation_preflight_delays) == {
            "failed_to_queued",
            "renew_live_lease",
        }
        for label, error in generation_errors.items():
            match = re.search(r"STORAGE_GENERATION_RETRY_BUSY:([0-9]+)", str(error))
            assert match is not None
            trigger_delay = int(match.group(1))
            assert trigger_delay <= generation_preflight_delays[label]
            assert generation_preflight_delays[label] - trigger_delay <= 1
        assert wrong_bucket_preflight_count == race_reserved_count
        expected_provider_objects = {
            (bucket, str(identity["object_key"])) for identity in domain_storage_identities.values()
        }
        assert set(s3.deleted) == expected_provider_objects
        assert len(s3.deleted) == race_reserved_count
        assert set(s3.headed) == expected_provider_objects
        assert len(s3.headed) == race_reserved_count * 2
        assert s3.operations == [
            operation
            for deleted_bucket, deleted_key in s3.deleted
            for operation in (
                ("head", deleted_bucket, deleted_key),
                ("delete", deleted_bucket, deleted_key),
                ("head", deleted_bucket, deleted_key),
            )
        ]
        assert s3.objects == {}

        with bootstrap.connect() as connection:
            recovery_state = connection.execute(
                text(
                    "SELECT maintenance_token, "
                    "maintenance_database_started_at IS NULL, "
                    "reserved_bytes, reserved_count, "
                    "recovery_database_started_at = pg_postmaster_start_time(), "
                    "recovery_completed_at IS NOT NULL "
                    "FROM storage_global_quotas WHERE id = 1"
                )
            ).one()
            assert recovery_state == (None, True, 0, 0, True, True)
            assert connection.execute(
                text(
                    "SELECT status, lease_token, lease_expires_at, error, "
                    "finished_at IS NOT NULL FROM generation_jobs WHERE id = :id"
                ),
                {"id": generation_job_id},
            ).one() == (
                "failed",
                None,
                None,
                "storage recovery reclaimed expired staging reservation",
                True,
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM stored_objects "
                        "WHERE organization_id = :organization_id"
                    ),
                    {"organization_id": organization_id},
                ).scalar_one()
                == 0
            )
            assert connection.execute(
                text(
                    "SELECT reserved_bytes, reserved_count FROM storage_tenant_quotas "
                    "WHERE organization_id = :organization_id"
                ),
                {"organization_id": organization_id},
            ).one() == (0, 0)
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM imported_assets "
                        "WHERE organization_id = :organization_id"
                    ),
                    {"organization_id": organization_id},
                ).scalar_one()
                == 0
            )

        with bootstrap.begin() as connection:
            _attest_current_capacity(connection, bucket=bucket)

        ordered_claim = {
            "project_id": project_id,
            "object_key": ordered_object_key,
            "sha256": ordered_sha256,
            "size_bytes": ordered_size_bytes,
            "media_type": "application/octet-stream",
            "owner_type": "imported_asset",
            "owner_id": ordered_asset_id,
            "idempotency_key": f"imported:{ordered_asset_id}",
        }
        ordered_lease_token = str(uuid.uuid4())
        with api.begin() as connection:
            _set_tenant(connection, organization_id)
            reservation = _call_storage_batch(
                connection,
                "custombuild_storage_reserve_batch",
                organization_id,
                [ordered_claim],
                ordered_lease_token,
                120,
            )
            assert isinstance(reservation, dict)
            assert reservation["newly_reserved_count"] == 1
            connection.execute(
                text(
                    "INSERT INTO imported_assets ("
                    "id, organization_id, project_id, sha256, object_key, size_bytes, "
                    "media_type, original_filename, created_by, created_at, updated_at"
                    ") VALUES ("
                    ":id, :organization_id, :project_id, :sha256, :object_key, "
                    ":size_bytes, 'application/octet-stream', 'ordered-reference.bin', "
                    ":created_by, clock_timestamp(), clock_timestamp())"
                ),
                {
                    "id": ordered_asset_id,
                    "organization_id": organization_id,
                    "project_id": project_id,
                    "sha256": ordered_sha256,
                    "object_key": ordered_object_key,
                    "size_bytes": ordered_size_bytes,
                    "created_by": user_id,
                },
            )
            committed = _call_storage_batch(
                connection,
                "custombuild_storage_commit_batch",
                organization_id,
                [ordered_claim],
                ordered_lease_token,
            )
            assert committed is None

        with bootstrap.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT state, sha256, size_bytes FROM stored_objects "
                    "WHERE organization_id = :organization_id AND object_key = :object_key"
                ),
                {"organization_id": organization_id, "object_key": ordered_object_key},
            ).one() == ("committed", ordered_sha256, ordered_size_bytes)
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM imported_assets "
                        "WHERE organization_id = :organization_id AND id = :id"
                    ),
                    {"organization_id": organization_id, "id": ordered_asset_id},
                ).scalar_one()
                == 1
            )

        with bootstrap.begin() as connection:
            connection.execute(
                text("DELETE FROM storage_tenant_quotas WHERE organization_id = :organization_id"),
                {"organization_id": organization_id},
            )
            connection.execute(
                text(
                    "INSERT INTO stored_objects ("
                    "organization_id, object_key, project_id, sha256, size_bytes, media_type, "
                    "owner_type, owner_id, idempotency_key, state, lease_token, "
                    "lease_expires_at, claim_token, claim_expires_at, created_at, updated_at"
                    ") VALUES ("
                    ":organization_id, :object_key, :project_id, :sha256, :size_bytes, "
                    "'application/octet-stream', 'integration_probe', :owner_id, "
                    ":idempotency_key, 'reserved', :lease_token, "
                    "clock_timestamp() - INTERVAL '1 minute', NULL, NULL, "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {
                    "organization_id": organization_id,
                    "object_key": missing_object_key,
                    "project_id": project_id,
                    "sha256": missing_sha256,
                    "size_bytes": missing_size_bytes,
                    "owner_id": str(uuid.uuid4()),
                    "idempotency_key": f"missing-quota:{suffix}",
                    "lease_token": str(uuid.uuid4()),
                },
            )
            connection.execute(
                text(
                    "UPDATE storage_global_quotas SET reserved_bytes = :size_bytes, "
                    "reserved_count = 1, updated_at = clock_timestamp() WHERE id = 1"
                ),
                {"size_bytes": missing_size_bytes},
            )

        missing_quota_token = str(uuid.uuid4())
        missing_quota_epoch = store.begin(missing_quota_token, bucket)
        with pytest.raises(
            StorageRecoveryError,
            match="stored objects exist without a tenant quota row",
        ):
            store.finish(missing_quota_token, missing_quota_epoch)
        with bootstrap.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT maintenance_token, maintenance_epoch, reserved_bytes, "
                    "reserved_count FROM storage_global_quotas WHERE id = 1"
                )
            ).one() == (
                missing_quota_token,
                missing_quota_epoch,
                missing_size_bytes,
                1,
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM stored_objects "
                        "WHERE organization_id = :organization_id "
                        "AND object_key = :object_key"
                    ),
                    {
                        "organization_id": organization_id,
                        "object_key": missing_object_key,
                    },
                ).scalar_one()
                == 1
            )
    finally:
        release_job_winner.set()
        if job_wins_thread is not None and job_wins_thread.is_alive():
            job_wins_thread.join(timeout=12)
        for domain_thread in domain_threads:
            if domain_thread.is_alive():
                domain_thread.join(timeout=12)
        for generation_thread in generation_threads:
            if generation_thread.is_alive():
                generation_thread.join(timeout=12)
        if global_before is not None:
            with bootstrap.begin() as connection:
                connection.execute(
                    text("DELETE FROM artifacts WHERE organization_id = :organization_id"),
                    {"organization_id": organization_id},
                )
                connection.execute(
                    text("DELETE FROM external_evidence WHERE organization_id = :organization_id"),
                    {"organization_id": organization_id},
                )
                connection.execute(
                    text("DELETE FROM imported_assets WHERE organization_id = :organization_id"),
                    {"organization_id": organization_id},
                )
                connection.execute(
                    text("DELETE FROM stored_objects WHERE organization_id = :organization_id"),
                    {"organization_id": organization_id},
                )
                connection.execute(
                    text("DELETE FROM organizations WHERE id = :organization_id"),
                    {"organization_id": organization_id},
                )
                connection.execute(
                    text("DELETE FROM users WHERE id = :user_id"),
                    {"user_id": user_id},
                )
                connection.execute(
                    text(
                        "UPDATE storage_global_quotas SET "
                        "byte_limit = :byte_limit, object_limit = :object_limit, "
                        "reserved_bytes = :reserved_bytes, reserved_count = :reserved_count, "
                        "committed_bytes = :committed_bytes, "
                        "committed_count = :committed_count, "
                        "capacity_verified = :capacity_verified, "
                        "provisioned_bytes = :provisioned_bytes, "
                        "metadata_overhead_bytes = :metadata_overhead_bytes, "
                        "emergency_reserve_bytes = :emergency_reserve_bytes, "
                        "capacity_headroom_bytes = :capacity_headroom_bytes, "
                        "volume_identity = :volume_identity, "
                        "capacity_bucket = :capacity_bucket, "
                        "capacity_operator_config_sha256 = "
                        ":capacity_operator_config_sha256, "
                        "deploy_descriptor_sha256 = :deploy_descriptor_sha256, "
                        "inventory_sha256 = :inventory_sha256, "
                        "inventory_object_count = :inventory_object_count, "
                        "inventory_bytes = :inventory_bytes, "
                        "ledger_object_count = :ledger_object_count, "
                        "ledger_bytes = :ledger_bytes, "
                        "capacity_attested_at = :capacity_attested_at, "
                        "capacity_verified_at = :capacity_verified_at, "
                        "capacity_evidence_sha256 = :capacity_evidence_sha256, "
                        "maintenance_token = :maintenance_token, "
                        "maintenance_epoch = :maintenance_epoch, "
                        "maintenance_started_at = :maintenance_started_at, "
                        "maintenance_owner_expires_at = :maintenance_owner_expires_at, "
                        "maintenance_database_started_at = "
                        ":maintenance_database_started_at, "
                        "recovery_database_started_at = :recovery_database_started_at, "
                        "recovery_completed_at = :recovery_completed_at, "
                        "updated_at = :updated_at WHERE id = 1"
                    ),
                    global_before,
                )
        api.dispose()
        worker.dispose()
        migrator.dispose()
        bootstrap.dispose()


_DOMAIN_REFERENCE_TABLES = ("imported_assets", "external_evidence", "artifacts")
_STORAGE_IDENTITY_MISMATCHES = (
    "project_id",
    "owner_type",
    "owner_id",
    "idempotency_key",
    "sha256",
    "size_bytes",
    "media_type",
)


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("reference_table", "mismatch_field"),
    [
        (reference_table, mismatch_field)
        for reference_table in _DOMAIN_REFERENCE_TABLES
        for mismatch_field in _STORAGE_IDENTITY_MISMATCHES
    ],
    ids=[
        f"{reference_table}-{mismatch_field}"
        for reference_table in _DOMAIN_REFERENCE_TABLES
        for mismatch_field in _STORAGE_IDENTITY_MISMATCHES
    ],
)
def test_deferred_domain_reference_trigger_rejects_every_identity_mismatch(
    reference_table: str,
    mismatch_field: str,
) -> None:
    bootstrap_url, _, api_url, worker_url = _urls()
    suffix = uuid.uuid4().hex[:12]
    organization_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    alternate_project_id = str(uuid.uuid4())
    design_version_id = str(uuid.uuid4())
    generation_job_id = str(uuid.uuid4())
    reference_id = str(uuid.uuid4())
    object_key = f"integration/{organization_id}/{reference_table}-{mismatch_field}.bin"
    reference_sha256 = "1" * 64
    reference_size_bytes = 101
    reference_media_type = "application/octet-stream"
    artifact_kind = "integration"

    expected_owner_type = {
        "imported_assets": "imported_asset",
        "external_evidence": "external_evidence",
        "artifacts": "generation_job",
    }[reference_table]
    expected_owner_id = generation_job_id if reference_table == "artifacts" else reference_id
    expected_idempotency_key = {
        "imported_assets": f"imported:{reference_id}",
        "external_evidence": f"external-evidence:{reference_id}",
        "artifacts": f"generation:{generation_job_id}:{artifact_kind}:{reference_id}",
    }[reference_table]
    stored_identity: dict[str, object] = {
        "project_id": project_id,
        "owner_type": expected_owner_type,
        "owner_id": expected_owner_id,
        "idempotency_key": expected_idempotency_key,
        "sha256": reference_sha256,
        "size_bytes": reference_size_bytes,
        "media_type": reference_media_type,
    }
    mismatched_values: dict[str, object] = {
        "project_id": alternate_project_id,
        "owner_type": "mismatched_owner",
        "owner_id": str(uuid.uuid4()),
        "idempotency_key": f"mismatched:{reference_id}",
        "sha256": "2" * 64,
        "size_bytes": reference_size_bytes + 1,
        "media_type": "application/x-mismatched",
    }
    stored_identity[mismatch_field] = mismatched_values[mismatch_field]

    bootstrap = create_engine(bootstrap_url)
    runtime = create_engine(
        worker_url if reference_table == "artifacts" else api_url,
        connect_args={"options": "-c statement_timeout=10000"},
    )
    global_before: tuple[int, int, object] | None = None
    statement_executed = False
    try:
        with bootstrap.begin() as connection:
            global_row = connection.execute(
                text(
                    "SELECT committed_bytes, committed_count, byte_limit, object_limit, "
                    "updated_at "
                    "FROM storage_global_quotas WHERE id = 1 FOR UPDATE"
                )
            ).one()
            global_before = (
                int(global_row.committed_bytes),
                int(global_row.committed_count),
                global_row.updated_at,
            )
            assert global_row.committed_bytes + int(stored_identity["size_bytes"]) <= (
                global_row.byte_limit
            )
            assert global_row.committed_count + 1 <= global_row.object_limit
            connection.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, created_at, updated_at) "
                    "VALUES (:id, 'Reference identity tenant', :slug, "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {"id": organization_id, "slug": f"reference-identity-{suffix}"},
            )
            connection.execute(
                text(
                    "INSERT INTO users (id, oidc_sub, email, name, created_at, updated_at) "
                    "VALUES (:id, :subject, :email, 'Reference identity user', "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {
                    "id": user_id,
                    "subject": f"reference-identity-{suffix}",
                    "email": f"reference-identity-{suffix}@example.test",
                },
            )
            for current_project_id, name in (
                (project_id, "Reference identity project"),
                (alternate_project_id, "Alternate reference project"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO projects ("
                        "id, organization_id, name, description, furniture_type, "
                        "current_revision, draft_revision, archived, created_at, updated_at"
                        ") VALUES ("
                        ":id, :organization_id, :name, '', 'bookcase', 0, 0, false, "
                        "clock_timestamp(), clock_timestamp())"
                    ),
                    {
                        "id": current_project_id,
                        "organization_id": organization_id,
                        "name": name,
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO design_versions ("
                    "id, organization_id, project_id, revision, status, design_hash, "
                    "context_hash, spec_json, source_provenance_json, result_json, "
                    "engine_version, template_version, template_id, "
                    "template_capability_fingerprint, rule_version, created_by, immutable, "
                    "created_at, updated_at"
                    ") VALUES ("
                    ":id, :organization_id, :project_id, 1, 'draft', :design_hash, "
                    ":context_hash, CAST(:empty_json AS json), CAST(:empty_json AS json), "
                    "CAST(:empty_json AS json), 'integration-engine', '1', 'shelving', "
                    ":template_fingerprint, 'integration-rules', :created_by, false, "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {
                    "id": design_version_id,
                    "organization_id": organization_id,
                    "project_id": project_id,
                    "design_hash": "3" * 64,
                    "context_hash": "4" * 64,
                    "empty_json": "{}",
                    "template_fingerprint": "5" * 64,
                    "created_by": user_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO generation_jobs ("
                    "id, organization_id, design_version_id, status, idempotency_key, "
                    "production_context_hash, production_engine_context_json, request_json, "
                    "result_json, attempts, lease_token, lease_expires_at, deadline_at, error, "
                    "started_at, finished_at, created_at, updated_at"
                    ") VALUES ("
                    ":id, :organization_id, :design_version_id, 'failed', "
                    ":idempotency_key, :context_hash, CAST(:empty_json AS json), "
                    "CAST(:empty_json AS json), NULL, 1, NULL, NULL, NULL, "
                    "'integration fixture', NULL, clock_timestamp(), "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {
                    "id": generation_job_id,
                    "organization_id": organization_id,
                    "design_version_id": design_version_id,
                    "idempotency_key": f"reference-{suffix}",
                    "context_hash": "6" * 64,
                    "empty_json": "{}",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO storage_tenant_quotas ("
                    "organization_id, byte_limit, object_limit, reserved_bytes, "
                    "committed_bytes, reserved_count, committed_count, created_at, updated_at"
                    ") VALUES ("
                    ":organization_id, 1000000, 1000, 0, :size_bytes, 0, 1, "
                    "clock_timestamp(), clock_timestamp())"
                ),
                {
                    "organization_id": organization_id,
                    "size_bytes": stored_identity["size_bytes"],
                },
            )
            connection.execute(
                text(
                    "INSERT INTO stored_objects ("
                    "organization_id, object_key, project_id, sha256, size_bytes, media_type, "
                    "owner_type, owner_id, idempotency_key, state, lease_token, "
                    "lease_expires_at, claim_token, claim_expires_at, created_at, updated_at"
                    ") VALUES ("
                    ":organization_id, :object_key, :project_id, :sha256, :size_bytes, "
                    ":media_type, :owner_type, :owner_id, :idempotency_key, 'committed', "
                    "NULL, NULL, NULL, NULL, clock_timestamp(), clock_timestamp())"
                ),
                {
                    "organization_id": organization_id,
                    "object_key": object_key,
                    **stored_identity,
                },
            )
            connection.execute(
                text(
                    "UPDATE storage_global_quotas SET "
                    "committed_bytes = committed_bytes + :size_bytes, "
                    "committed_count = committed_count + 1, updated_at = clock_timestamp() "
                    "WHERE id = 1"
                ),
                {"size_bytes": stored_identity["size_bytes"]},
            )

        statements = {
            "imported_assets": (
                "INSERT INTO imported_assets ("
                "id, organization_id, project_id, sha256, object_key, size_bytes, "
                "media_type, original_filename, created_by, created_at, updated_at"
                ") VALUES ("
                ":id, :organization_id, :project_id, :sha256, :object_key, :size_bytes, "
                ":media_type, 'identity-mismatch.bin', :created_by, "
                "clock_timestamp(), clock_timestamp())"
            ),
            "external_evidence": (
                "INSERT INTO external_evidence ("
                "id, organization_id, project_id, evidence_type, rule_id, catalog_id, "
                "catalog_version, design_hash, object_key, sha256, size_bytes, content_type, "
                "created_by, expires_at, revoked_at, created_at, updated_at"
                ") VALUES ("
                ":id, :organization_id, :project_id, 'certificate', 'integration-rule', "
                "'integration-catalog', '1', :design_hash, :object_key, :sha256, "
                ":size_bytes, :media_type, :created_by, NULL, NULL, "
                "clock_timestamp(), clock_timestamp())"
            ),
            "artifacts": (
                "INSERT INTO artifacts ("
                "id, organization_id, generation_job_id, kind, object_key, sha256, "
                "size_bytes, content_type, created_at, updated_at"
                ") VALUES ("
                ":id, :organization_id, :generation_job_id, :kind, :object_key, "
                ":sha256, :size_bytes, :media_type, clock_timestamp(), clock_timestamp())"
            ),
        }
        with (
            pytest.raises(DBAPIError, match="STORAGE_DOMAIN_REFERENCE_INVALID"),
            runtime.begin() as connection,
        ):
            _set_tenant(connection, organization_id)
            inserted = connection.execute(
                text(statements[reference_table]),
                {
                    "id": reference_id,
                    "organization_id": organization_id,
                    "project_id": project_id,
                    "generation_job_id": generation_job_id,
                    "kind": artifact_kind,
                    "object_key": object_key,
                    "sha256": reference_sha256,
                    "size_bytes": reference_size_bytes,
                    "media_type": reference_media_type,
                    "created_by": user_id,
                    "design_hash": "7" * 64,
                },
            )
            assert inserted.rowcount == 1
            statement_executed = True
        assert statement_executed
    finally:
        if global_before is not None:
            with bootstrap.begin() as connection:
                for table_name in ("imported_assets", "external_evidence", "artifacts"):
                    connection.execute(
                        text(
                            f"DELETE FROM {table_name} "  # noqa: S608 - fixed allow-list
                            "WHERE organization_id = :organization_id"
                        ),
                        {"organization_id": organization_id},
                    )
                connection.execute(
                    text("DELETE FROM stored_objects WHERE organization_id = :organization_id"),
                    {"organization_id": organization_id},
                )
                connection.execute(
                    text("DELETE FROM organizations WHERE id = :organization_id"),
                    {"organization_id": organization_id},
                )
                connection.execute(
                    text("DELETE FROM users WHERE id = :user_id"),
                    {"user_id": user_id},
                )
                connection.execute(
                    text(
                        "UPDATE storage_global_quotas SET committed_bytes = :committed_bytes, "
                        "committed_count = :committed_count, updated_at = :updated_at "
                        "WHERE id = 1"
                    ),
                    {
                        "committed_bytes": global_before[0],
                        "committed_count": global_before[1],
                        "updated_at": global_before[2],
                    },
                )
        runtime.dispose()
        bootstrap.dispose()
