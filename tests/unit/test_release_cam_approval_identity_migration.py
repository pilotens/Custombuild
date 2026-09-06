from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models import (
    RELEASE_CAM_APPROVAL_RECEIPT_CHECK_SQL,
    RELEASE_CAM_APPROVAL_UUID_CHECK_SQL,
    Approval,
    Release,
)
from sqlalchemy.exc import IntegrityError


def _migration() -> Any:
    return importlib.import_module(
        "services.api.alembic.versions.0020_release_cam_approval_identity"
    )


INVALID_UUIDS = (
    "55555555-5555-9555-8555-555555555555",  # UUID version 9
    "55555555-5555-4555-7555-555555555555",  # UUID variant 7
    "55555555-5555-4555-8555-55555555555-",  # fifth/extra hyphen
    "55555555-5555-4555-8555-55555555555z",  # non-hex scalar
)


def _assert_invalid_receipts_rejected(
    connection: sa.Connection,
    *,
    table: sa.Table,
    valid_approval_id: str,
) -> None:
    for invalid_uuid in INVALID_UUIDS:
        with pytest.raises(IntegrityError):
            connection.execute(
                table.update()
                .where(table.c.id == "legacy-release")
                .values(
                    cam_approval_id=invalid_uuid,
                    cam_approval_binding_sha256="a" * 64,
                    cam_approval_snapshot_json={},
                )
            )
    with pytest.raises(IntegrityError):
        connection.execute(
            table.update()
            .where(table.c.id == "legacy-release")
            .values(
                cam_approval_id=valid_approval_id,
                cam_approval_binding_sha256="z" * 64,
                cam_approval_snapshot_json={},
            )
        )


def test_postgres_downgrade_guard_enumerates_force_rls_tenants() -> None:
    migration = _migration()

    class ScalarRows:
        def __init__(self, values: tuple[str, ...] = ()) -> None:
            self.values = values

        def scalars(self) -> tuple[str, ...]:
            return self.values

    class ForceRLSBind:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self) -> None:
            self.current_organization_id = ""
            self.tenant_contexts: list[str] = []

        def execute(
            self,
            statement: sa.Executable,
            parameters: dict[str, str] | None = None,
        ) -> ScalarRows:
            sql = str(statement)
            if "SELECT id FROM organizations" in sql:
                return ScalarRows(("tenant-a", "tenant-b"))
            if "app.current_organization_id" in sql:
                organization_id = (parameters or {}).get("organization_id", "")
                self.current_organization_id = organization_id
                self.tenant_contexts.append(organization_id)
            return ScalarRows()

        def scalar(self, statement: sa.Executable) -> int:
            assert "FROM public.releases" in str(statement)
            return {"tenant-a": 0, "tenant-b": 1}[self.current_organization_id]

    bind = ForceRLSBind()
    original_op = migration.op
    migration.op = SimpleNamespace(get_bind=lambda: bind)
    try:
        with pytest.raises(
            RuntimeError,
            match="EXECUTABLE_RELEASE_RECEIPT_DOWNGRADE_BLOCKED",
        ):
            migration.downgrade()
    finally:
        migration.op = original_op

    assert bind.tenant_contexts == ["tenant-a", "tenant-b", ""]
    assert bind.current_organization_id == ""


def test_create_all_model_has_the_same_composite_approval_boundary() -> None:
    approval_unique_names = {constraint.name for constraint in Approval.__table__.constraints}
    assert "uq_approvals_org_version_job_id" in approval_unique_names
    release_constraints = {
        constraint.name: constraint for constraint in Release.__table__.constraints
    }
    foreign_key = release_constraints["fk_releases_org_version_job_cam_approval"]
    assert tuple(foreign_key.column_keys) == (
        "organization_id",
        "design_version_id",
        "generation_job_id",
        "cam_approval_id",
    )
    assert tuple(element.target_fullname for element in foreign_key.elements) == (
        "approvals.organization_id",
        "approvals.design_version_id",
        "approvals.generation_job_id",
        "approvals.id",
    )
    assert foreign_key.ondelete == "RESTRICT"
    assert {
        "cam_approval_id",
        "cam_approval_binding_sha256",
        "cam_approval_snapshot_json",
    } <= set(Release.__table__.columns.keys())
    assert "ck_releases_cam_approval_receipt_complete" in release_constraints
    migration = _migration()
    assert (
        str(release_constraints["ck_releases_cam_approval_id_format"].sqltext)
        == migration.UUID_CHECK_SQL
        == RELEASE_CAM_APPROVAL_UUID_CHECK_SQL
    )
    assert (
        str(release_constraints["ck_releases_cam_approval_receipt_complete"].sqltext)
        == migration.RECEIPT_CHECK_SQL
        == RELEASE_CAM_APPROVAL_RECEIPT_CHECK_SQL
    )


def test_create_all_constraints_reject_noncanonical_uuid_and_sha_values() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    probe = sa.Table(
        "release_constraint_probe",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cam_approval_id", sa.String(36), nullable=True),
        sa.Column("cam_approval_binding_sha256", sa.String(64), nullable=True),
        sa.Column("cam_approval_snapshot_json", sa.JSON(none_as_null=True), nullable=True),
        sa.CheckConstraint(RELEASE_CAM_APPROVAL_UUID_CHECK_SQL),
        sa.CheckConstraint(RELEASE_CAM_APPROVAL_RECEIPT_CHECK_SQL),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO release_constraint_probe (id) VALUES ('legacy-release')")
        )
        _assert_invalid_receipts_rejected(
            connection,
            table=probe,
            valid_approval_id="55555555-5555-4555-8555-555555555555",
        )
    engine.dispose()


def test_release_cam_approval_identity_is_nullable_for_history_and_loss_safe() -> None:
    migration = _migration()
    assert migration.revision == "0020_release_cam_approval_identity"
    assert migration.down_revision == "0019_cam_approval_candidate_sha"

    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "approvals",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("design_version_id", sa.String(36), nullable=False),
        sa.Column("generation_job_id", sa.String(36), nullable=True),
    )
    sa.Table(
        "releases",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("design_version_id", sa.String(36), nullable=False),
        sa.Column("generation_job_id", sa.String(36), nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys = ON"))
        organization_id = "11111111-1111-4111-8111-111111111111"
        other_organization_id = "22222222-2222-4222-8222-222222222222"
        design_version_id = "33333333-3333-4333-8333-333333333333"
        job_id = "44444444-4444-4444-8444-444444444444"
        approval_id = "55555555-5555-4555-8555-555555555555"
        for candidate_approval_id in (approval_id, *INVALID_UUIDS):
            connection.execute(
                sa.text(
                    "INSERT INTO approvals "
                    "(id, organization_id, design_version_id, generation_job_id) "
                    "VALUES (:id, :organization_id, :design_version_id, :job_id)"
                ),
                {
                    "id": candidate_approval_id,
                    "organization_id": organization_id,
                    "design_version_id": design_version_id,
                    "job_id": job_id,
                },
            )
        connection.execute(
            sa.text(
                "INSERT INTO releases "
                "(id, organization_id, design_version_id, generation_job_id) "
                "VALUES ('legacy-release', :organization_id, :design_version_id, :job_id)"
            ),
            {
                "organization_id": organization_id,
                "design_version_id": design_version_id,
                "job_id": job_id,
            },
        )
        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
            columns = {
                column["name"]: column for column in sa.inspect(connection).get_columns("releases")
            }
            assert columns["cam_approval_id"]["nullable"] is True
            assert (
                connection.scalar(
                    sa.text("SELECT cam_approval_id FROM releases WHERE id = 'legacy-release'")
                )
                is None
            )
            constraint_names = {
                constraint["name"]
                for constraint in sa.inspect(connection).get_check_constraints("releases")
            }
            assert migration.UUID_CHECK_NAME in constraint_names
            assert migration.RECEIPT_CHECK_NAME in constraint_names
            assert migration.APPROVAL_UNIQUE_NAME in {
                constraint["name"]
                for constraint in sa.inspect(connection).get_unique_constraints("approvals")
            }
            release_fks = {
                constraint["name"]: constraint
                for constraint in sa.inspect(connection).get_foreign_keys("releases")
            }
            assert release_fks[migration.APPROVAL_FK_NAME]["options"] == {"ondelete": "RESTRICT"}

            _assert_invalid_receipts_rejected(
                connection,
                table=sa.Table("releases", sa.MetaData(), autoload_with=connection),
                valid_approval_id=approval_id,
            )

            connection.execute(
                sa.text(
                    "UPDATE releases SET cam_approval_id = :approval_id, "
                    "cam_approval_binding_sha256 = :binding_sha256, "
                    "cam_approval_snapshot_json = '{}' WHERE id = 'legacy-release'"
                ),
                {"approval_id": approval_id, "binding_sha256": "a" * 64},
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    sa.text(
                        "UPDATE releases SET organization_id = :other_organization_id "
                        "WHERE id = 'legacy-release'"
                    ),
                    {"other_organization_id": other_organization_id},
                )
            with pytest.raises(IntegrityError):
                connection.execute(
                    sa.text("DELETE FROM approvals WHERE id = :approval_id"),
                    {"approval_id": approval_id},
                )
            with pytest.raises(
                RuntimeError,
                match="EXECUTABLE_RELEASE_RECEIPT_DOWNGRADE_BLOCKED",
            ):
                migration.downgrade()
            connection.execute(
                sa.text(
                    "UPDATE releases SET cam_approval_id = NULL, "
                    "cam_approval_binding_sha256 = NULL, "
                    "cam_approval_snapshot_json = NULL WHERE id = 'legacy-release'"
                )
            )
            migration.downgrade()
            assert "cam_approval_id" not in {
                column["name"] for column in sa.inspect(connection).get_columns("releases")
            }
        finally:
            migration.op = original_op
    engine.dispose()
