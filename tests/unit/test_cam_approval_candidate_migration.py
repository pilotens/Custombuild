from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models import Approval
from sqlalchemy.exc import IntegrityError


def _migration() -> Any:
    return importlib.import_module("services.api.alembic.versions.0019_cam_approval_candidate_sha")


def test_create_all_candidate_digest_constraint_requires_exact_lowercase_hex() -> None:
    constraint = next(
        item
        for item in Approval.__table__.constraints
        if item.name == "ck_approvals_cam_candidate_bundle_sha256_format"
    )
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    probe = sa.Table(
        "approval_digest_probe",
        metadata,
        sa.Column("cam_candidate_bundle_sha256", sa.String(64), nullable=True),
        sa.CheckConstraint(str(constraint.sqltext)),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(probe.insert().values(cam_candidate_bundle_sha256="a" * 64))
        with pytest.raises(IntegrityError):
            connection.execute(probe.insert().values(cam_candidate_bundle_sha256="g" * 64))
    engine.dispose()


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
            assert "FROM public.approvals" in str(statement)
            return {"tenant-a": 0, "tenant-b": 1}[self.current_organization_id]

    bind = ForceRLSBind()
    original_op = migration.op
    migration.op = SimpleNamespace(get_bind=lambda: bind)
    try:
        with pytest.raises(
            RuntimeError,
            match="CAM_APPROVAL_CANDIDATE_BINDING_DOWNGRADE_BLOCKED",
        ):
            migration.downgrade()
    finally:
        migration.op = original_op

    assert bind.tenant_contexts == ["tenant-a", "tenant-b", ""]
    assert bind.current_organization_id == ""


def test_candidate_digest_migration_leaves_old_approvals_unbound_and_is_loss_safe() -> None:
    migration = _migration()
    assert migration.revision == "0019_cam_approval_candidate_sha"
    assert migration.down_revision == "0018_joint_retention_registry_state"

    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "approvals",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("approval_type", sa.String(80), nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO approvals (id, approval_type) "
                "VALUES ('legacy-cam', 'cam'), ('legacy-design', 'design')"
            )
        )
        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
            columns = {
                column["name"]: column for column in sa.inspect(connection).get_columns("approvals")
            }
            assert columns["cam_candidate_bundle_sha256"]["nullable"] is True
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM approvals "
                        "WHERE cam_candidate_bundle_sha256 IS NOT NULL"
                    )
                )
                == 0
            )
            assert migration.CHECK_NAME in {
                constraint["name"]
                for constraint in sa.inspect(connection).get_check_constraints("approvals")
            }
            with pytest.raises(IntegrityError):
                connection.execute(
                    sa.text(
                        "UPDATE approvals SET cam_candidate_bundle_sha256 = :digest "
                        "WHERE id = 'legacy-cam'"
                    ),
                    {"digest": "z" * 64},
                )
            connection.execute(
                sa.text(
                    "UPDATE approvals SET cam_candidate_bundle_sha256 = :digest "
                    "WHERE id = 'legacy-cam'"
                ),
                {"digest": "a" * 64},
            )
            with pytest.raises(
                RuntimeError,
                match="CAM_APPROVAL_CANDIDATE_BINDING_DOWNGRADE_BLOCKED",
            ):
                migration.downgrade()
            connection.execute(
                sa.text(
                    "UPDATE approvals SET cam_candidate_bundle_sha256 = NULL "
                    "WHERE id = 'legacy-cam'"
                )
            )
            migration.downgrade()
            assert "cam_candidate_bundle_sha256" not in {
                column["name"] for column in sa.inspect(connection).get_columns("approvals")
            }
        finally:
            migration.op = original_op
    engine.dispose()
