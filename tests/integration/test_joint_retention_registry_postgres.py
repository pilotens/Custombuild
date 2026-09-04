from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from collections.abc import Mapping
from typing import Any

import pytest
from app.joint_retention_registry import joint_retention_registry_binding
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from scripts import activate_joint_retention_registry as activation

INSTALL_SIGNATURE = (
    "public.custombuild_joint_retention_install_registry(jsonb,text,text,text)"
)
ASSERT_SIGNATURE = "public.custombuild_joint_retention_assert_registry(text,text)"


def _postgres_engines() -> tuple[Engine, Engine, Engine, Engine] | None:
    values = tuple(
        os.getenv(name)
        for name in (
            "MIGRATION_DATABASE_URL",
            "RLS_DATABASE_URL",
            "WORKER_RLS_DATABASE_URL",
            "CAPACITY_ATTESTOR_DATABASE_URL",
        )
    )
    if any(value is None for value in values):
        return None
    migrator = activation._engine(str(values[0]))
    application_engines = tuple(
        create_engine(str(value)) for value in values[1:]
    )
    return (migrator, *application_engines)  # type: ignore[return-value]


def _registry() -> dict[str, Any]:
    return {
        "schema_version": "custombuild.joint-retention-trust-registry.v1",
        "issuers": [
            {
                "issuer_id": "postgres-integration-lab",
                "key_id": "ed25519-2026-01",
                "role": "joint_retention_certifier",
                "public_key_base64": base64.b64encode(bytes(range(32))).decode("ascii"),
                "not_before": "2026-01-01T00:00:00Z",
                "not_after": "2028-01-01T00:00:00Z",
                "revoked_at": None,
            }
        ],
        "revoked_statement_sha256": [],
        "revoked_system_versions": [],
    }


def _install(
    connection: Connection,
    registry: Mapping[str, Any],
) -> tuple[int, bool]:
    binding = joint_retention_registry_binding(registry)
    row = connection.execute(
        text(
            "SELECT activated_epoch, changed FROM "
            "public.custombuild_joint_retention_install_registry("
            "CAST(:registry_json AS pg_catalog.jsonb), :canonical_json, "
            ":registry_sha256, :operator_reference_sha256)"
        ),
        {
            "registry_json": binding.canonical_json,
            "canonical_json": binding.canonical_json,
            "registry_sha256": binding.sha256,
            "operator_reference_sha256": hashlib.sha256(
                b"postgres-integration-change"
            ).hexdigest(),
        },
    ).one()
    return int(row[0]), bool(row[1])


def _install_raw(
    connection: Connection,
    registry: Mapping[str, Any],
) -> tuple[int, bool]:
    canonical_json = json.dumps(
        registry,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    row = connection.execute(
        text(
            "SELECT activated_epoch, changed FROM "
            "public.custombuild_joint_retention_install_registry("
            "CAST(:registry_json AS pg_catalog.jsonb), :canonical_json, "
            ":registry_sha256, :operator_reference_sha256)"
        ),
        {
            "registry_json": canonical_json,
            "canonical_json": canonical_json,
            "registry_sha256": hashlib.sha256(canonical_json.encode()).hexdigest(),
            "operator_reference_sha256": hashlib.sha256(
                b"postgres-integration-change"
            ).hexdigest(),
        },
    ).one()
    return int(row[0]), bool(row[1])


def _assert_registry(connection: Connection, registry: Mapping[str, Any]) -> int:
    binding = joint_retention_registry_binding(registry)
    return int(
        connection.scalar(
            text(
                "SELECT public.custombuild_joint_retention_assert_registry("
                ":canonical_json, :registry_sha256)"
            ),
            {
                "canonical_json": binding.canonical_json,
                "registry_sha256": binding.sha256,
            },
        )
    )


@pytest.mark.postgres
def test_live_registry_acl_monotonicity_idempotence_and_lock_serialization() -> None:
    engines = _postgres_engines()
    if engines is None:
        pytest.skip("PostgreSQL registry probe requires migrator and all runtime roles")
    migrator, api, worker, attestor = engines
    baseline = _registry()
    candidate = copy.deepcopy(baseline)
    candidate["revoked_statement_sha256"] = ["a" * 64]
    try:
        with migrator.connect() as connection:
            functions = connection.execute(
                text(
                    "SELECT procedure.oid::regprocedure::text, procedure.prosecdef, "
                    "procedure.proconfig, owner.rolname "
                    "FROM pg_catalog.pg_proc procedure "
                    "JOIN pg_catalog.pg_roles owner ON owner.oid = procedure.proowner "
                    "WHERE procedure.oid = ANY(CAST(:functions AS regprocedure[])) "
                    "ORDER BY procedure.oid::regprocedure::text"
                ),
                {"functions": [INSTALL_SIGNATURE, ASSERT_SIGNATURE]},
            ).all()
        assert len(functions) == 2
        assert all(row[1] is True for row in functions)
        assert all(row[2] == ["search_path=pg_catalog, public"] for row in functions)
        assert all(row[3] == "custombuild_migrator" for row in functions)

        with migrator.connect() as connection:
            database_owner = connection.scalar(
                text(
                    "SELECT owner.rolname FROM pg_catalog.pg_database database "
                    "JOIN pg_catalog.pg_roles owner ON owner.oid = database.datdba "
                    "WHERE database.datname = pg_catalog.current_database()"
                )
            )
            assert isinstance(database_owner, str)
            quoted_owner = connection.dialect.identifier_preparer.quote(database_owner)
        unexpected_acl_cases = (
            (
                "GRANT SELECT ON public.joint_retention_registry_state TO PUBLIC",
                "REVOKE SELECT ON public.joint_retention_registry_state FROM PUBLIC",
            ),
            (
                "GRANT EXECUTE ON FUNCTION "
                "public.custombuild_joint_retention_assert_registry(text,text) TO "
                f"{quoted_owner}",
                "REVOKE EXECUTE ON FUNCTION "
                "public.custombuild_joint_retention_assert_registry(text,text) FROM "
                f"{quoted_owner}",
            ),
            (
                "GRANT UPDATE (registry_sha256) ON "
                "public.joint_retention_registry_state TO "
                f"{quoted_owner}",
                "REVOKE UPDATE (registry_sha256) ON "
                "public.joint_retention_registry_state FROM "
                f"{quoted_owner}",
            ),
            (
                "GRANT EXECUTE ON FUNCTION "
                "public.custombuild_joint_retention_assert_registry(text,text) "
                "TO custombuild_api WITH GRANT OPTION",
                "REVOKE GRANT OPTION FOR EXECUTE ON FUNCTION "
                "public.custombuild_joint_retention_assert_registry(text,text) "
                "FROM custombuild_api",
            ),
        )
        for grant_sql, revoke_sql in unexpected_acl_cases:
            with migrator.begin() as connection:
                connection.execute(text(grant_sql))
                with pytest.raises(
                    activation.RegistryActivationError,
                    match="unexpected direct ACL",
                ):
                    activation._guard_database_connection(connection)  # type: ignore[arg-type]
                connection.execute(text(revoke_sql))

        for engine in (api, worker, attestor):
            with engine.connect() as connection:
                assert connection.execute(
                    text(
                        "SELECT "
                        "pg_catalog.has_table_privilege(current_user, "
                        "'public.joint_retention_registry_state', 'SELECT'), "
                        "pg_catalog.has_table_privilege(current_user, "
                        "'public.joint_retention_registry_state', 'INSERT'), "
                        "pg_catalog.has_table_privilege(current_user, "
                        "'public.joint_retention_registry_state', 'UPDATE'), "
                        "pg_catalog.has_table_privilege(current_user, "
                        "'public.joint_retention_registry_state', 'DELETE'), "
                        "pg_catalog.has_function_privilege(current_user, "
                        ":install, 'EXECUTE'), "
                        "pg_catalog.has_function_privilege(current_user, "
                        ":assertion, 'EXECUTE')"
                    ),
                    {"install": INSTALL_SIGNATURE, "assertion": ASSERT_SIGNATURE},
                ).one() == (
                    False,
                    False,
                    False,
                    False,
                    False,
                    engine is not attestor,
                )

        with migrator.begin() as connection:
            baseline_epoch, changed = _install(connection, baseline)
        assert changed is True
        assert baseline_epoch >= 1

        for revoked in (False, True):
            aliased = copy.deepcopy(baseline)
            if revoked:
                aliased["issuers"][0]["revoked_at"] = "2026-09-03T12:00:00Z"
            alias = copy.deepcopy(aliased["issuers"][0])
            alias["key_id"] = "ed25519-2027-alias"
            alias["revoked_at"] = None
            aliased["issuers"].append(alias)
            with (
                migrator.connect() as connection,
                pytest.raises(DBAPIError, match="key material"),
            ):
                _install_raw(connection, aliased)

        # A runtime transaction holds a shared policy snapshot.  Activation
        # must wait rather than crossing that transaction with mixed trust.
        with api.connect() as runtime_connection:
            assert _assert_registry(runtime_connection, baseline) == baseline_epoch
            with migrator.connect() as activation_connection:
                activation_connection.execute(text("SET LOCAL lock_timeout = '100ms'"))
                with pytest.raises(DBAPIError):
                    _install(activation_connection, candidate)

        with migrator.begin() as connection:
            candidate_epoch, changed = _install(connection, candidate)
        assert changed is True
        assert candidate_epoch == baseline_epoch + 1

        with migrator.begin() as connection:
            replay_epoch, changed = _install(connection, candidate)
        assert changed is False
        assert replay_epoch == candidate_epoch

        revoked = copy.deepcopy(candidate)
        revoked["issuers"][0]["revoked_at"] = "2027-01-01T00:00:00Z"
        with migrator.begin() as connection:
            revoked_epoch, changed = _install(connection, revoked)
        assert changed is True
        assert revoked_epoch == candidate_epoch + 1

        tightened = copy.deepcopy(revoked)
        tightened["issuers"][0]["revoked_at"] = "2026-09-03T12:00:00Z"
        with migrator.begin() as connection:
            tightened_epoch, changed = _install(connection, tightened)
        assert changed is True
        assert tightened_epoch == revoked_epoch + 1

        offset_equivalent = copy.deepcopy(tightened)
        offset_equivalent["issuers"][0]["revoked_at"] = "2026-09-03T14:00:00+02:00"
        with migrator.begin() as connection:
            offset_epoch, changed = _install(connection, offset_equivalent)
        assert changed is True
        assert offset_epoch == tightened_epoch + 1

        for rejected_revocation in (None, "2026-09-04T12:00:00Z"):
            rolled_back = copy.deepcopy(offset_equivalent)
            rolled_back["issuers"][0]["revoked_at"] = rejected_revocation
            with (
                migrator.connect() as connection,
                pytest.raises(DBAPIError, match="revocation"),
            ):
                _install(connection, rolled_back)

        with (
            migrator.connect() as connection,
            pytest.raises(DBAPIError, match="ROLLBACK"),
        ):
            _install(connection, baseline)
        for engine in (api, worker):
            with engine.connect() as connection:
                assert _assert_registry(connection, offset_equivalent) == offset_epoch
                with pytest.raises(DBAPIError):
                    _assert_registry(connection, baseline)
    finally:
        for engine in engines:
            engine.dispose()
