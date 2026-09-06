from __future__ import annotations

import importlib
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from scripts import bootstrap_production_identity


def _migration() -> Any:
    return importlib.import_module(
        "services.api.alembic.versions.0017_oidc_issuer_binding"
    )


def test_revision_follows_workshop_persistence_and_leaves_legacy_rows_unbound() -> None:
    migration = _migration()
    assert migration.revision == "0017_oidc_issuer_binding"
    assert migration.down_revision == "0016_workshop_trust_persistence"
    assert migration.IDENTITY_BOOTSTRAP_LOCK_ID == bootstrap_production_identity.BOOTSTRAP_LOCK_ID

    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "users",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("oidc_sub", sa.String(255), nullable=False, unique=True),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO users (id, oidc_sub) VALUES (:id, :subject)"),
            {"id": "legacy-user", "subject": "raw-legacy-subject"},
        )
        operations = Operations(MigrationContext.configure(connection))
        original_op = migration.op
        migration.op = operations
        try:
            migration.upgrade()
            columns = {
                column["name"]: column for column in sa.inspect(connection).get_columns("users")
            }
            assert columns["oidc_issuer_sha256"]["nullable"] is True
            assert connection.scalar(
                sa.text(
                    "SELECT oidc_issuer_sha256 FROM users WHERE id = 'legacy-user'"
                )
            ) is None
            assert migration.INDEX_NAME in {
                index["name"] for index in sa.inspect(connection).get_indexes("users")
            }
            assert migration.CHECK_NAME in {
                constraint["name"]
                for constraint in sa.inspect(connection).get_check_constraints("users")
            }
            with pytest.raises(IntegrityError):
                connection.execute(
                    sa.text(
                        "INSERT INTO users (id, oidc_sub, oidc_issuer_sha256) "
                        "VALUES ('invalid-user', 'opaque-key', :invalid_hash)"
                    ),
                    {"invalid_hash": "z" * 64},
                )
            connection.execute(
                sa.text(
                    "UPDATE users SET oidc_issuer_sha256 = :issuer_hash "
                    "WHERE id = 'legacy-user'"
                ),
                {"issuer_hash": "a" * 64},
            )
            with pytest.raises(
                RuntimeError,
                match="OIDC_ISSUER_BINDING_DOWNGRADE_BLOCKED",
            ):
                migration.downgrade()
            assert "oidc_issuer_sha256" in {
                column["name"] for column in sa.inspect(connection).get_columns("users")
            }
            connection.execute(
                sa.text(
                    "UPDATE users SET oidc_issuer_sha256 = NULL "
                    "WHERE id = 'legacy-user'"
                )
            )
            migration.downgrade()
            assert "oidc_issuer_sha256" not in {
                column["name"] for column in sa.inspect(connection).get_columns("users")
            }
        finally:
            migration.op = original_op
    engine.dispose()
