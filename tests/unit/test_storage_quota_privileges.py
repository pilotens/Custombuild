from __future__ import annotations

import importlib
import os
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from scripts.postgres_runtime_privileges import (
    API_TABLE_PRIVILEGES,
    ROLE_FUNCTION_PRIVILEGES,
    STORAGE_API_RETRY_SIGNATURES,
    STORAGE_ATTESTOR_SIGNATURES,
    STORAGE_ATTESTOR_TABLE_PRIVILEGES,
    STORAGE_QUOTA_MUTATOR_SIGNATURES,
    STORAGE_REAPER_SIGNATURES,
    WORKER_TABLE_PRIVILEGES,
    runtime_privilege_statements,
    storage_quota_function_privilege_statements,
)

MIGRATION = Path("services/api/alembic/versions/0013_storage_quota_security_functions.py")
QUOTA_RUNTIME = Path("services/api/app/storage_quota.py")
REAPER_RUNTIME = Path("services/api/app/storage_reaper.py")
API_RUNTIME = Path("services/api/app/api.py")
LEDGER_TABLES = (
    "storage_global_quotas",
    "storage_tenant_quotas",
    "stored_objects",
)


def test_storage_tables_are_read_only_to_both_untrusted_runtimes() -> None:
    for privileges in (API_TABLE_PRIVILEGES, WORKER_TABLE_PRIVILEGES):
        for table in LEDGER_TABLES:
            assert privileges[table] == ("SELECT",)

    sql = ";\n".join(runtime_privilege_statements())
    assert "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC" in sql
    assert (
        "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public "
        "FROM custombuild_api, custombuild_worker"
    ) in sql
    assert (
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator "
        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    ) in runtime_privilege_statements()
    assert (
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator "
        "REVOKE EXECUTE ON FUNCTIONS FROM custombuild_api, custombuild_worker, "
        "custombuild_storage_attestor"
    ) in runtime_privilege_statements()
    assert "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC" in sql


def test_api_readiness_can_only_read_alembic_revision_metadata() -> None:
    assert API_TABLE_PRIVILEGES["alembic_version"] == ("SELECT",)
    assert "alembic_version" not in WORKER_TABLE_PRIVILEGES

    sql = ";\n".join(runtime_privilege_statements())
    assert "GRANT SELECT ON TABLE alembic_version" in sql
    assert "alembic_version TO custombuild_worker" not in sql


def test_only_public_storage_entry_points_are_granted() -> None:
    api_functions = ROLE_FUNCTION_PRIVILEGES["custombuild_api"]
    worker_functions = ROLE_FUNCTION_PRIVILEGES["custombuild_worker"]

    assert api_functions == STORAGE_QUOTA_MUTATOR_SIGNATURES + STORAGE_API_RETRY_SIGNATURES
    assert worker_functions == STORAGE_QUOTA_MUTATOR_SIGNATURES + STORAGE_REAPER_SIGNATURES
    assert not any("attest" in signature for signature in api_functions + worker_functions)
    assert not any("invalidate" in signature for signature in api_functions + worker_functions)
    assert not any("._custombuild" in signature for signature in api_functions)

    grants = storage_quota_function_privilege_statements()
    assert all("GRANT EXECUTE ON FUNCTION" in grant for grant in grants)
    assert all(
        any(signature in grant for grant in grants)
        for signature in (
            *STORAGE_QUOTA_MUTATOR_SIGNATURES,
            *STORAGE_API_RETRY_SIGNATURES,
            *STORAGE_REAPER_SIGNATURES,
        )
    )
    assert all(
        any(signature in grant for grant in grants) for signature in STORAGE_ATTESTOR_SIGNATURES
    )
    attestor_sql = ";\n".join((*runtime_privilege_statements(), *grants))
    assert (
        "GRANT SELECT ON TABLE organizations, storage_global_quotas, "
        "storage_object_tombstones, storage_tenant_quotas, stored_objects "
        "TO custombuild_storage_attestor"
    ) in attestor_sql
    attestor_grants = tuple(
        statement
        for statement in runtime_privilege_statements()
        if statement.endswith("TO custombuild_storage_attestor")
    )
    assert all("INSERT" not in statement for statement in attestor_grants)
    assert all("UPDATE" not in statement for statement in attestor_grants)
    assert all("DELETE" not in statement for statement in attestor_grants)


def test_migration_definer_functions_have_frozen_search_path_and_db_clock() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision = "0012_storage_quota_ledger"' in source
    created = re.findall(
        r"CREATE OR REPLACE FUNCTION public\.([^(]+)\([^$]+?\$function\$",
        source,
        flags=re.DOTALL,
    )
    assert len(created) == 20
    assert source.count("SECURITY DEFINER") == 20
    assert source.count("SET search_path TO pg_catalog, public") == 20
    assert "SET search_path TO public, pg_catalog" not in source
    assert "pg_catalog.clock_timestamp()" in source
    assert "capacity_verified_at < v_now - INTERVAL '10 minutes'" in source
    assert "capacity_attested_at > v_global.capacity_verified_at" in source
    assert "capacity_headroom_bytes" in source
    assert "provisioned_bytes - v_global.capacity_headroom_bytes" in source
    assert "inventory_object_count <> v_global.ledger_object_count" in source
    assert "inventory_bytes <> v_global.ledger_bytes" in source
    assert "ledger_object_count <> v_global.committed_count" in source
    assert "ledger_bytes <> v_global.committed_bytes" in source
    assert "custombuild_storage_lock_capacity" in source


def test_migration_uses_postgresql_conditional_expressions_without_schema_qualification() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert re.findall(
        r"\bpg_catalog\.(?:coalesce|greatest|least|nullif)\s*\(",
        source,
        flags=re.IGNORECASE,
    ) == []
    assert "COALESCE(p_name, 'uuid')" in source
    assert "COALESCE(p_name, 'text')" in source
    assert "COALESCE(v_keys_allowed, false)" in source
    assert source.count("GREATEST(lease_expires_at, v_expiry)") == 2
    assert source.count("GREATEST(v_retry_after, v_candidate)") == 2


def test_reservation_function_validates_exact_batch_before_mutating() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    reserve_start = source.index("_CREATE_RESERVE =")
    renew_start = source.index("_CREATE_RENEW =")
    reserve = source[reserve_start:renew_start]

    assert "claim keys do not match the canonical schema" in source
    assert "duplicate batch key" in source
    assert "^[0-9a-f]{64}$" in source
    assert "size_bytes exceeds the canonical" in source
    assert "tenant byte limit" in source
    assert reserve.index("_custombuild_storage_assert_claims") < reserve.index(
        "INSERT INTO public.stored_objects"
    )
    assert reserve.index("capacity_verified IS DISTINCT FROM true") < reserve.index(
        "INSERT INTO public.stored_objects"
    )
    assert "FOR UPDATE" in reserve
    assert "whole batch does not fit" in reserve
    assert "reserved_bytes = reserved_bytes + v_byte_delta" in reserve
    assert "FROM public.storage_object_tombstones AS tombstone" in reserve
    assert "tombstone.capacity_bucket = v_global.capacity_bucket" in reserve
    assert "tombstone.object_key = v_claim ->> 'object_key'" in reserve
    assert "tombstone.idempotency_key = v_claim ->> 'idempotency_key'" in reserve
    assert "physical storage key or " in reserve
    assert "STORAGE_GENERATION_RETRY_BUSY:" in reserve
    assert "v_candidate := 5" in reserve
    assert "SET state = 'reserved'" not in reserve


def test_postgresql_runtime_uses_only_security_definer_storage_mutators() -> None:
    quota_source = QUOTA_RUNTIME.read_text(encoding="utf-8")
    reaper_source = REAPER_RUNTIME.read_text(encoding="utf-8")

    for function_name in (
        "custombuild_storage_reserve_batch",
        "custombuild_storage_renew_batch",
        "custombuild_storage_commit_batch",
    ):
        assert f"SELECT public.{function_name}" in quota_source
    for function_name in (
        "custombuild_storage_claim_expired_reservations",
        "custombuild_storage_claim_delete_pending",
        "custombuild_storage_finalize_reap",
    ):
        assert function_name in reaper_source
    assert 'dialect.name == "postgresql"' in quota_source
    assert 'dialect.name == "postgresql"' in reaper_source


def test_release_archive_never_row_locks_append_only_or_read_only_tables() -> None:
    source = API_RUNTIME.read_text(encoding="utf-8")
    resolver = source[
        source.index("def _resolve_release_archive(") : source.index(
            "def _verify_release_archive_owned("
        )
    ]
    release = source[
        source.index("def release_version(") : source.index(
            '@router.get(\n    "/releases/{release_id}/artifacts"'
        )
    ]

    assert "select(Release)" in resolver
    assert "select(Artifact)" in resolver
    assert "select(StoredObject)" in resolver
    assert (
        ".with_for_update()"
        not in resolver.split("select(Release)", 1)[1].split("select(DesignVersion)", 1)[0]
    )
    assert ".with_for_update()" not in resolver.split("select(Artifact)", 1)[1]
    assert ".with_for_update()" not in release.split("release_artifacts =", 1)[1]


def test_reaper_is_token_bound_and_never_debits_before_finalization() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    claim_start = source.index("_CREATE_CLAIM_REAP_HELPER =")
    finalize_start = source.index("_CREATE_FINALIZE_REAP =")
    claim = source[claim_start:finalize_start]
    finalize_end = source.index("_CREATE_ATTEST_CAPACITY =")
    finalize = source[finalize_start:finalize_end]

    assert "FOR UPDATE OF candidate SKIP LOCKED" in claim
    assert (
        "ORDER BY COALESCE(\n"
        "                     candidate.lease_expires_at, candidate.claim_expires_at,\n"
        "                     candidate.updated_at\n"
        "                 ), candidate.object_key"
    ) in claim
    assert "candidate.lease_expires_at <= v_now" in claim
    assert "candidate.claim_expires_at <= v_now" in claim
    assert "NOT EXISTS" in claim
    assert "generation_job.status IN ('queued', 'running')" in claim
    assert "candidate.state IN ('committed', 'delete_pending')" in claim
    assert "reserved_bytes = reserved_bytes -" not in claim
    assert "committed_bytes = committed_bytes -" not in claim
    assert "claim_token = v_effective_token" in claim
    assert "v_marker NOT IN ('4', '5')" in finalize
    assert "v_row.claim_token IS DISTINCT FROM p_claim_token" in finalize
    assert "v_row.claim_expires_at <= v_now" in finalize
    assert "reserved counters would underflow" in finalize
    assert "committed counters would underflow" in finalize
    assert "object still has a domain reference" in finalize
    assert "generation_job.status IN ('queued', 'running')" in finalize
    assert "AND claim_token = p_claim_token" in finalize
    tombstone_insert = finalize.index("INSERT INTO public.storage_object_tombstones")
    counter_debit = min(
        finalize.index("reserved_bytes = reserved_bytes - p_size_bytes"),
        finalize.index("committed_bytes = committed_bytes - p_size_bytes"),
    )
    live_delete = finalize.index("DELETE FROM public.stored_objects")
    assert tombstone_insert < counter_debit < live_delete
    assert "p_capacity_bucket, v_row.object_key" in finalize
    assert "v_row.idempotency_key" in finalize


def test_deferred_domain_reference_triggers_close_reaper_provider_race() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    trigger_function = source.split("_CREATE_ENFORCE_DOMAIN_REFERENCE =", 1)[1].split(
        "_CREATE_FINALIZE_REAP =", 1
    )[0]

    assert "RETURNS trigger" in trigger_function
    assert "stored.state = 'committed'" in trigger_function
    assert "stored.project_id IS NOT DISTINCT FROM v_project_id" in trigger_function
    assert "stored.sha256 IS NOT DISTINCT FROM NEW.sha256" in trigger_function
    assert "stored.size_bytes IS NOT DISTINCT FROM NEW.size_bytes" in trigger_function
    assert "stored.media_type IS NOT DISTINCT FROM v_media_type" in trigger_function
    assert "stored.owner_type IS NOT DISTINCT FROM v_owner_type" in trigger_function
    assert "stored.owner_id IS NOT DISTINCT FROM v_owner_id" in trigger_function
    assert "stored.idempotency_key IS NOT DISTINCT FROM v_idempotency_key" in trigger_function
    assert "'imported:' || NEW.id" in trigger_function
    assert "'external-evidence:' || NEW.id" in trigger_function
    assert "|| NEW.kind || ':' || NEW.id" in trigger_function
    assert "JOIN public.design_versions AS design_version" in trigger_function
    assert "STORAGE_DOMAIN_REFERENCE_INVALID" in trigger_function
    assert source.count("CREATE CONSTRAINT TRIGGER") == 1
    assert "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW" in source
    assert set(
        dict(
            importlib.import_module(
                "services.api.alembic.versions.0013_storage_quota_security_functions"
            )._DOMAIN_REFERENCE_TRIGGERS
        )
    ) == {
        "imported_assets",
        "external_evidence",
        "artifacts",
    }


def test_immediate_generation_liveness_trigger_blocks_reaper_retry_races() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    trigger_function = source.split("_CREATE_ENFORCE_GENERATION_LIVENESS =", 1)[1].split(
        "_CREATE_FINALIZE_REAP =", 1
    )[0]

    assert "NEW.status IN ('queued', 'running')" in trigger_function
    assert "NEW.lease_expires_at IS NOT NULL" in trigger_function
    assert "stored.owner_type = 'generation_job'" in trigger_function
    assert "stored.owner_id = NEW.id" in trigger_function
    assert "ORDER BY stored.object_key" in trigger_function
    assert "FOR KEY SHARE" in trigger_function
    assert "v_row.state = 'reaping'" in trigger_function
    assert "('reserved', 'committed', 'delete_pending')" in trigger_function
    assert "v_row.claim_expires_at > v_now" in trigger_function
    assert "v_candidate := 5" in trigger_function
    assert "STORAGE_GENERATION_RETRY_BUSY:" in trigger_function
    assert "STORAGE_GENERATION_LIVENESS_INVALID" in trigger_function
    assert "CREATE TRIGGER {_GENERATION_LIVENESS_TRIGGER}" in source
    assert "BEFORE INSERT OR UPDATE ON public.generation_jobs" in source
    generation_trigger = source.split('f"CREATE TRIGGER {_GENERATION_LIVENESS_TRIGGER} "', 1)[
        1
    ].split(")", 1)[0]
    assert "DEFERRABLE" not in generation_trigger


def test_generation_retry_preflight_locks_terminal_job_then_storage_and_bounds_delay() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    preflight = source.split("_CREATE_PREPARE_GENERATION_RETRY =", 1)[1].split(
        "_CREATE_ENFORCE_GENERATION_LIVENESS =", 1
    )[0]

    assert "custombuild_storage_prepare_generation_retry" in preflight
    assert "RETURNS integer" in preflight
    assert "SECURITY DEFINER" in preflight
    assert "SET search_path TO pg_catalog, public" in preflight
    assert "_custombuild_storage_require_tenant(p_organization_id)" in preflight
    assert "_custombuild_storage_assert_uuid(" in preflight
    job_lock = preflight.index("FROM public.generation_jobs AS generation_job")
    storage_lock = preflight.index("PERFORM stored.object_key")
    database_clock = preflight.index("v_now := pg_catalog.clock_timestamp()")
    assert job_lock < storage_lock < database_clock
    assert "v_job_status NOT IN ('failed', 'succeeded')" in preflight
    assert "ORDER BY stored.object_key" in preflight
    assert "FOR UPDATE" in preflight
    assert "v_row.claim_expires_at > v_now" in preflight
    assert "EXTRACT(EPOCH FROM (v_row.claim_expires_at - v_now))" in preflight
    assert "v_candidate < 1 OR v_candidate > 3605" in preflight
    assert "GREATEST(v_retry_after, v_candidate)" in preflight
    assert "('reserved', 'committed', 'delete_pending')" in preflight
    assert "v_candidate := 5" in preflight
    assert "an invalid reaper claim" in preflight


def test_retired_storage_keys_are_append_only_and_hidden_from_runtimes() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    privileges = ";\n".join(runtime_privilege_statements())

    assert "_CREATE_REJECT_TOMBSTONE_MUTATION" in source
    assert "BEFORE UPDATE OR DELETE ON public.storage_object_tombstones" in source
    assert "STORAGE_TOMBSTONE_IMMUTABLE" in source
    assert "storage_object_tombstones" not in API_TABLE_PRIVILEGES
    assert "storage_object_tombstones" not in WORKER_TABLE_PRIVILEGES
    assert STORAGE_ATTESTOR_TABLE_PRIVILEGES["storage_object_tombstones"] == ("SELECT",)
    assert "storage_object_tombstones TO custombuild_api" not in privileges
    assert "storage_object_tombstones TO custombuild_worker" not in privileges


def test_normal_reaper_binds_attested_bucket_before_provider_io() -> None:
    migration = MIGRATION.read_text(encoding="utf-8")
    runtime = REAPER_RUNTIME.read_text(encoding="utf-8")
    bucket_function = migration.split("_CREATE_ASSERT_REAP_BUCKET =", 1)[1].split(
        "_CREATE_CLAIM_DELETE_PENDING =", 1
    )[0]
    preflight = runtime.split("def _postgresql_reap_preflight(", 1)[1].split(
        "def _finalize_postgresql_reap(", 1
    )[0]
    finalize = migration.split("_CREATE_FINALIZE_REAP =", 1)[1].split("_CREATE_LOCK_CAPACITY =", 1)[
        0
    ]
    reap = runtime.split("def reap_storage_claim(", 1)[1]

    assert "FOR UPDATE" in bucket_function
    assert "v_capacity_bucket IS DISTINCT FROM p_capacity_bucket" in bucket_function
    assert "STORAGE_BUCKET_MISMATCH" in bucket_function
    assert "custombuild_storage_assert_reap_bucket" in preflight
    assert preflight.index("custombuild_storage_assert_reap_bucket") < preflight.index(
        "select(StoredObject)"
    )
    assert reap.index("_postgresql_reap_preflight(") < reap.index("_delete_and_confirm_missing(")
    assert "p_capacity_bucket text" in finalize
    assert "v_global.capacity_bucket IS DISTINCT FROM p_capacity_bucket" in finalize
    assert finalize.index("STORAGE_BUCKET_MISMATCH") < finalize.index(
        "reserved_bytes = reserved_bytes - p_size_bytes"
    )
    assert ":capacity_bucket)" in runtime


def test_upgrade_revokes_helpers_and_never_grants_attestation_to_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module(
        "services.api.alembic.versions.0013_storage_quota_security_functions"
    )
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda statement: statements.append(statement))

    migration.upgrade()

    sql = ";\n".join(statements)
    managed_signatures = (
        *migration._HELPER_FUNCTIONS,
        *migration._PUBLIC_FUNCTIONS,
        *migration._API_FUNCTIONS,
        *migration._ATTESTOR_FUNCTIONS,
    )
    for signature in managed_signatures:
        assert statements.count(f"ALTER FUNCTION {signature} OWNER TO custombuild_migrator") == 1
        assert statements.count(f"REVOKE ALL PRIVILEGES ON FUNCTION {signature} FROM PUBLIC") == 1
    assert (
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator "
        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    ) in statements
    assert (
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator "
        "REVOKE EXECUTE ON FUNCTIONS FROM custombuild_api, custombuild_worker, "
        "custombuild_storage_attestor"
    ) in statements
    assert (
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    ) in sql
    first_function = next(
        index
        for index, statement in enumerate(statements)
        if "CREATE OR REPLACE FUNCTION public." in str(statement)
    )
    global_public_revoke = statements.index(
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator "
        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    )
    global_runtime_revoke = statements.index(
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator "
        "REVOKE EXECUTE ON FUNCTIONS FROM custombuild_api, custombuild_worker, "
        "custombuild_storage_attestor"
    )
    assert global_public_revoke < first_function
    assert global_runtime_revoke < first_function
    assert "must be provisioned before migration 0013" in sql
    assert "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC" in sql
    assert (
        "REVOKE ALL PRIVILEGES ON TABLE storage_global_quotas, "
        "storage_tenant_quotas, stored_objects, storage_object_tombstones "
        "FROM custombuild_api, custombuild_worker"
    ) in sql
    assert (
        "GRANT SELECT ON TABLE storage_global_quotas, storage_tenant_quotas, "
        "stored_objects TO custombuild_api, custombuild_worker"
    ) in sql
    assert "GRANT SELECT ON TABLE alembic_version TO custombuild_api" in sql
    assert "GRANT EXECUTE ON FUNCTION public.custombuild_storage_reserve_batch" in sql
    for signature in migration._HELPER_FUNCTIONS:
        assert f"REVOKE ALL PRIVILEGES ON FUNCTION {signature} FROM PUBLIC" in sql
        assert f"GRANT EXECUTE ON FUNCTION {signature} TO custombuild_api" not in sql
        assert f"GRANT EXECUTE ON FUNCTION {signature} TO custombuild_worker" not in sql
    for signature in migration._API_FUNCTIONS:
        assert f"GRANT EXECUTE ON FUNCTION {signature} TO custombuild_api" in sql
        assert f"GRANT EXECUTE ON FUNCTION {signature} TO custombuild_worker" not in sql
    for signature in migration._ATTESTOR_FUNCTIONS:
        assert f"GRANT EXECUTE ON FUNCTION {signature} TO custombuild_api" not in sql
        assert f"GRANT EXECUTE ON FUNCTION {signature} TO custombuild_worker" not in sql
        assert f"GRANT EXECUTE ON FUNCTION {signature} TO custombuild_storage_attestor" in sql
    assert (
        "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM custombuild_storage_attestor"
    ) in sql


def test_downgrade_restores_pre_readiness_metadata_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module(
        "services.api.alembic.versions.0013_storage_quota_security_functions"
    )
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", lambda statement: statements.append(statement))

    migration.downgrade()

    assert statements.count(
        "REVOKE SELECT ON TABLE alembic_version FROM custombuild_api"
    ) == 1


def _postgres_urls() -> tuple[str, str, str, str] | None:
    migrator = os.getenv("MIGRATION_DATABASE_URL")
    api = os.getenv("RLS_DATABASE_URL")
    worker = os.getenv("WORKER_RLS_DATABASE_URL")
    attestor = os.getenv("CAPACITY_ATTESTOR_DATABASE_URL")
    if not all((migrator, api, worker, attestor)):
        return None
    assert migrator is not None and api is not None and worker is not None
    assert attestor is not None
    return migrator, api, worker, attestor


@pytest.mark.postgres
def test_live_postgres_storage_function_ownership_and_acl() -> None:
    urls = _postgres_urls()
    if urls is None:
        pytest.skip("PostgreSQL storage privilege probe requires all runtime roles")
    migrator_url, api_url, worker_url, attestor_url = urls
    migrator = create_engine(migrator_url)
    api = create_engine(api_url)
    worker = create_engine(worker_url)
    attestor = create_engine(attestor_url)
    all_entry_points = (
        *STORAGE_QUOTA_MUTATOR_SIGNATURES,
        *STORAGE_API_RETRY_SIGNATURES,
        *STORAGE_REAPER_SIGNATURES,
    )
    try:
        with migrator.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT procedure.oid::regprocedure::text, procedure.prosecdef, "
                    "procedure.proconfig, owner.rolname "
                    "FROM pg_catalog.pg_proc AS procedure "
                    "JOIN pg_catalog.pg_roles AS owner ON owner.oid = procedure.proowner "
                    "WHERE procedure.oid = ANY ("
                    "CAST(:functions AS regprocedure[])"
                    ") ORDER BY procedure.oid::regprocedure::text"
                ),
                {"functions": list(all_entry_points)},
            ).all()
        assert len(rows) == len(all_entry_points)
        assert all(row[1] is True for row in rows)
        assert all(row[2] == ["search_path=pg_catalog, public"] for row in rows)
        assert all(row[3] == "custombuild_migrator" for row in rows)

        for engine, role in ((api, "custombuild_api"), (worker, "custombuild_worker")):
            with engine.connect() as connection:
                for table in LEDGER_TABLES:
                    assert connection.execute(
                        text(
                            "SELECT has_table_privilege(current_user, :table, 'SELECT'), "
                            "has_table_privilege(current_user, :table, 'INSERT'), "
                            "has_table_privilege(current_user, :table, 'UPDATE'), "
                            "has_table_privilege(current_user, :table, 'DELETE')"
                        ),
                        {"table": f"public.{table}"},
                    ).one() == (True, False, False, False)
                for signature in all_entry_points:
                    expected = signature in ROLE_FUNCTION_PRIVILEGES[role]
                    assert (
                        connection.execute(
                            text(
                                "SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"
                            ),
                            {"signature": signature},
                        ).scalar_one()
                        is expected
                    )
                for signature in STORAGE_ATTESTOR_SIGNATURES:
                    assert (
                        connection.execute(
                            text(
                                "SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"
                            ),
                            {"signature": signature},
                        ).scalar_one()
                        is False
                    )
        with attestor.connect() as connection:
            direct_rows = connection.execute(
                text(
                    "SELECT table_name, privilege_type "
                    "FROM information_schema.role_table_grants "
                    "WHERE table_schema = 'public' AND grantee = current_user "
                    "ORDER BY table_name, privilege_type"
                )
            ).all()
            direct_privileges: dict[str, set[str]] = {}
            for table_name, privilege in direct_rows:
                direct_privileges.setdefault(str(table_name), set()).add(str(privilege))
            assert direct_privileges == {
                table: set(privileges)
                for table, privileges in STORAGE_ATTESTOR_TABLE_PRIVILEGES.items()
            }
            assert connection.execute(
                text(
                    "SELECT has_schema_privilege(current_user, 'public', 'USAGE'), "
                    "has_schema_privilege(current_user, 'public', 'CREATE')"
                )
            ).one() == (True, False)
            for table, privileges in STORAGE_ATTESTOR_TABLE_PRIVILEGES.items():
                assert privileges == ("SELECT",)
                assert connection.execute(
                    text(
                        "SELECT has_table_privilege(current_user, :table, 'SELECT'), "
                        "has_table_privilege(current_user, :table, 'INSERT'), "
                        "has_table_privilege(current_user, :table, 'UPDATE'), "
                        "has_table_privilege(current_user, :table, 'DELETE')"
                    ),
                    {"table": f"public.{table}"},
                ).one() == (True, False, False, False)
            for signature in STORAGE_ATTESTOR_SIGNATURES:
                assert (
                    connection.execute(
                        text("SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"),
                        {"signature": signature},
                    ).scalar_one()
                    is True
                )
            for signature in all_entry_points:
                assert (
                    connection.execute(
                        text("SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"),
                        {"signature": signature},
                    ).scalar_one()
                    is False
                )
            assert (
                connection.execute(
                    text(
                        "SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb "
                        "AND NOT rolcreaterole AND NOT rolinherit AND NOT rolreplication "
                        "AND NOT rolbypassrls FROM pg_roles WHERE rolname = current_user"
                    )
                ).scalar_one()
                is True
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_auth_members membership "
                        "JOIN pg_roles member ON member.oid = membership.member "
                        "JOIN pg_roles granted ON granted.oid = membership.roleid "
                        "WHERE member.rolname = current_user OR granted.rolname = current_user"
                    )
                ).scalar_one()
                == 0
            )
    finally:
        attestor.dispose()
        worker.dispose()
        api.dispose()
        migrator.dispose()
