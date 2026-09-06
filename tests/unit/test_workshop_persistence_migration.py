from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.db import Base
from app.models import (
    DesignVersion,
    GenerationJob,
    Release,
    WorkshopAcceptanceSigner,
    WorkshopChainAcceptance,
    WorkshopIssuerKey,
    WorkshopNonce,
    WorkshopNonceSet,
    WorkshopRevocation,
    WorkshopRunProgram,
    WorkshopRunRecord,
    WorkshopSignerPrincipal,
)

from scripts.postgres_runtime_privileges import (
    API_TABLE_PRIVILEGES,
    WORKER_TABLE_PRIVILEGES,
)

MIGRATION_PATH = Path(
    "services/api/alembic/versions/0016_workshop_trust_persistence.py"
)


def _migration() -> Any:
    return importlib.import_module(
        "services.api.alembic.versions.0016_workshop_trust_persistence"
    )


def _constraint_names(table: sa.Table) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint.name, str)
    }


def _database_unique_constraint_names(
    connection: sa.Connection,
    table_name: str,
) -> set[str]:
    return {
        name
        for constraint in sa.inspect(connection).get_unique_constraints(table_name)
        if isinstance((name := constraint.get("name")), str)
    }


def _drop_model_only_parent_constraints(
    connection: sa.Connection,
    migration: Any,
) -> None:
    """Turn current declarative metadata into the actual 0015 parent shape."""

    operations = Operations(MigrationContext.configure(connection))
    for table_name, constraint_name, _columns in migration._PARENT_CONSTRAINTS:
        with operations.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(constraint_name, type_="unique")


def test_revision_is_read_only_and_follows_retry_schedule() -> None:
    migration = _migration()
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert migration.revision == "0016_workshop_trust_persistence"
    assert migration.down_revision == "0015_outbox_retry_schedule"
    assert len(migration.TENANT_TABLES) == 12
    assert set(migration.IMMUTABLE_TABLES) == set(migration.TENANT_TABLES) - {
        "workshop_trust_states",
        "workshop_nonce_sets",
    }
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "current_setting('app.current_organization_id', true)" in source
    assert "GRANT SELECT ON TABLE" not in source
    assert "GRANT INSERT" not in source
    assert "GRANT UPDATE" not in source
    assert "GRANT DELETE" not in source
    assert "custombuild_workshop_reject_immutable_mutation" in source
    assert "WORKSHOP_PERSISTENCE_MIGRATION_CONFLICT" in source
    assert "SECURITY DEFINER" in source
    assert "SET search_path TO pg_catalog, public" in source
    assert "custombuild_workshop_finalize" not in source


def test_models_bind_exact_release_graph_and_only_executable_programs() -> None:
    run_table = cast(sa.Table, WorkshopRunRecord.__table__)
    run_constraints = _constraint_names(run_table)
    release_table = cast(sa.Table, Release.__table__)
    release_constraints = _constraint_names(release_table)

    assert "fk_workshop_runs_org_release_graph" in run_constraints
    release_fk = next(
        constraint
        for constraint in run_table.foreign_key_constraints
        if constraint.name == "fk_workshop_runs_org_release_graph"
    )
    assert tuple(release_fk.column_keys) == (
        "organization_id",
        "design_version_id",
        "generation_job_id",
        "design_review_release_id",
    )
    assert tuple(element.target_fullname for element in release_fk.elements) == (
        "releases.organization_id",
        "releases.design_version_id",
        "releases.generation_job_id",
        "releases.id",
    )
    assert "ck_workshop_runs_executable_only" in run_constraints
    assert "uq_design_versions_org_project_id" in _constraint_names(
        cast(sa.Table, DesignVersion.__table__)
    )
    assert "uq_generation_jobs_org_version_id" in _constraint_names(
        cast(sa.Table, GenerationJob.__table__)
    )
    assert "uq_releases_org_id" in release_constraints
    assert "uq_releases_org_version_job_id" in release_constraints


def test_program_members_preserve_exact_canonical_identity() -> None:
    program_table = cast(sa.Table, WorkshopRunProgram.__table__)
    required_columns = {
        "program_id",
        "purpose",
        "relative_path",
        "setup_id",
        "wcs_id",
        "stock_id",
        "operation_set_sha256",
        "program_sha256",
        "program_size_bytes",
        "media_type",
        "identity_sha256",
        "canonical_identity_json_bytes",
        "identity_size_bytes",
    }

    assert required_columns <= set(program_table.c.keys())
    assert "uq_workshop_run_programs_org_run_program_id" in _constraint_names(
        program_table
    )
    assert "uq_workshop_run_programs_org_run_path" in _constraint_names(program_table)
    assert "ck_workshop_run_programs_bytes" in _constraint_names(program_table)


def test_nonce_persistence_is_digest_only_and_rederivable() -> None:
    nonce_set = cast(sa.Table, WorkshopNonceSet.__table__)
    nonce = cast(sa.Table, WorkshopNonce.__table__)

    assert "nonce_derivation_context" in nonce_set.c
    assert isinstance(nonce_set.c.nonce_derivation_context.type, sa.LargeBinary)
    assert nonce_set.c.nonce_derivation_context.type.length == 32
    assert "nonce_key_version" in nonce_set.c
    assert "nonce_derivation_scheme" in nonce_set.c
    assert "generation" in nonce_set.c
    assert "nonce_digest_sha256" in nonce.c
    assert "server_nonce" not in nonce.c
    assert "raw_nonce" not in nonce.c
    assert "nonce" not in nonce.c
    assert "server_nonce" not in nonce_set.c
    assert "raw_nonce" not in nonce_set.c
    assert "uq_workshop_nonces_org_digest" in _constraint_names(nonce)
    assert "fk_workshop_nonces_org_set_binding" in _constraint_names(nonce)
    assert "uq_workshop_nonce_sets_org_run_generation" in _constraint_names(
        nonce_set
    )
    assert "uq_workshop_nonce_sets_active_run" in {
        index.name for index in nonce_set.indexes
    }


def test_acceptance_supports_supervisor_without_aliasing_maker_checker() -> None:
    principal_table = cast(sa.Table, WorkshopSignerPrincipal.__table__)
    key_table = cast(sa.Table, WorkshopIssuerKey.__table__)
    signer_table = cast(sa.Table, WorkshopAcceptanceSigner.__table__)
    acceptance_table = cast(sa.Table, WorkshopChainAcceptance.__table__)
    principal_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in principal_table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    key_columns = key_table.c
    signer_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in signer_table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }

    assert "workshop_supervisor" in principal_checks[
        "ck_workshop_signer_principals_role"
    ]
    assert "qualified_air_cut_supervisor" in key_columns
    assert "workshop_supervisor" in signer_checks[
        "ck_workshop_acceptance_signers_role"
    ]
    assert "stage = 'PRE_CUT'" in signer_checks[
        "ck_workshop_acceptance_signers_role"
    ]
    assert signer_table.primary_key.columns.keys() == [
        "organization_id",
        "acceptance_id",
        "stage",
        "signer_role",
    ]
    assert "uq_workshop_chain_acceptances_org_nonce_set" in _constraint_names(
        acceptance_table
    )


def test_revocations_are_epoch_serializable_and_chain_acceptance_is_not_run_unique() -> None:
    revocation_table = cast(sa.Table, WorkshopRevocation.__table__)
    acceptance_table = cast(sa.Table, WorkshopChainAcceptance.__table__)
    revocation_constraints = _constraint_names(revocation_table)
    acceptance_constraints = _constraint_names(acceptance_table)

    assert "uq_workshop_revocations_org_epoch" in revocation_constraints
    assert "uq_workshop_revocations_org_target" in revocation_constraints
    assert "uq_workshop_revocations_org_idempotency" in revocation_constraints
    revocation_check = next(
        str(constraint.sqltext)
        for constraint in revocation_table.constraints
        if constraint.name == "ck_workshop_revocations_target_kind"
        and isinstance(constraint, sa.CheckConstraint)
    )
    assert "EVIDENCE_ATTACHMENT" in revocation_check
    assert "POLICY" not in revocation_check
    assert "CHAIN" not in revocation_check
    assert "uq_workshop_chain_acceptances_org_run_chain" in acceptance_constraints
    assert not any(
        isinstance(constraint, sa.UniqueConstraint)
        and tuple(constraint.columns.keys()) == ("organization_id", "workshop_run_id")
        for constraint in acceptance_table.constraints
    )


def test_both_untrusted_runtimes_have_no_workshop_table_access() -> None:
    migration = _migration()

    for table_name in migration.TENANT_TABLES:
        assert table_name not in API_TABLE_PRIVILEGES
        assert table_name not in WORKER_TABLE_PRIVILEGES


def test_sqlite_metadata_upgrade_and_downgrade_match_workshop_models() -> None:
    migration = _migration()
    expected_tables = set(migration.TENANT_TABLES)
    engine = sa.create_engine("sqlite:///:memory:")
    pre_workshop_tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name not in expected_tables
    ]
    Base.metadata.create_all(engine, tables=pre_workshop_tables)

    with engine.begin() as connection:
        _drop_model_only_parent_constraints(connection, migration)
        for table_name, constraint_name, _columns in migration._PARENT_CONSTRAINTS:
            assert constraint_name not in _database_unique_constraint_names(
                connection,
                table_name,
            )
        connection.execute(
            sa.text(
                "INSERT INTO organizations "
                "(id, name, slug, created_at, updated_at) "
                "VALUES (:id, :name, :slug, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": "11111111-1111-4111-8111-111111111111", "name": "Tenant", "slug": "t"},
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

            inspector = sa.inspect(connection)
            assert expected_tables <= set(inspector.get_table_names())
            for table_name, constraint_name, _columns in migration._PARENT_CONSTRAINTS:
                assert constraint_name in _database_unique_constraint_names(
                    connection,
                    table_name,
                )
            state = connection.execute(
                sa.text(
                    "SELECT organization_id, trust_epoch "
                    "FROM workshop_trust_states"
                )
            ).one()
            assert tuple(state) == (
                "11111111-1111-4111-8111-111111111111",
                0,
            )

            for table_name in expected_tables:
                model_columns = {
                    column.name for column in Base.metadata.tables[table_name].columns
                }
                migrated_columns = {
                    column["name"] for column in inspector.get_columns(table_name)
                }
                assert migrated_columns == model_columns

            migration.downgrade()
            assert expected_tables.isdisjoint(
                sa.inspect(connection).get_table_names()
            )
            for table_name, constraint_name, _columns in migration._PARENT_CONSTRAINTS:
                assert constraint_name not in _database_unique_constraint_names(
                    connection,
                    table_name,
                )


def test_upgrade_rejects_pre_existing_parent_constraints_without_owning_them() -> None:
    migration = _migration()
    expected_tables = set(migration.TENANT_TABLES)
    engine = sa.create_engine("sqlite:///:memory:")
    pre_workshop_tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name not in expected_tables
    ]
    Base.metadata.create_all(engine, tables=pre_workshop_tables)

    with engine.begin() as connection:
        before = {
            table_name: _database_unique_constraint_names(connection, table_name)
            for table_name, _constraint_name, _columns in migration._PARENT_CONSTRAINTS
        }
        with (
            Operations.context(MigrationContext.configure(connection)),
            pytest.raises(
                RuntimeError,
                match="WORKSHOP_PERSISTENCE_MIGRATION_CONFLICT",
            ) as raised,
        ):
            migration.upgrade()

        assert "pre-existing parent constraints" in str(raised.value)
        assert expected_tables.isdisjoint(sa.inspect(connection).get_table_names())
        after = {
            table_name: _database_unique_constraint_names(connection, table_name)
            for table_name, _constraint_name, _columns in migration._PARENT_CONSTRAINTS
        }
        assert after == before
