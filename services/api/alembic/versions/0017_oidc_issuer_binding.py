"""Add an explicit issuer-binding marker for production OIDC identities.

Revision ID: 0017_oidc_issuer_binding
Revises: 0016_workshop_trust_persistence

Existing rows deliberately remain NULL. Production readiness and authentication
reject those legacy or pre-marker identities until an operator binds each exact
provider subject with the audited production identity bootstrap CLI.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_oidc_issuer_binding"
down_revision = "0016_workshop_trust_persistence"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_users_oidc_issuer_sha256"
CHECK_NAME = "ck_users_oidc_issuer_sha256_format"
IDENTITY_BOOTSTRAP_LOCK_ID = 4_340_449_326_452_121_807


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "SELECT pg_catalog.set_config("
                "'search_path', 'pg_catalog,public', true)"
            )
        )
    op.add_column(
        "users",
        sa.Column(
            "oidc_issuer_sha256",
            sa.String(length=64),
            nullable=True,
        ),
    )
    if bind.dialect.name == "postgresql":
        check_expression = (
            "oidc_issuer_sha256 IS NULL "
            "OR oidc_issuer_sha256 ~ '^[0-9a-f]{64}$'"
        )
    else:
        check_expression = (
            "oidc_issuer_sha256 IS NULL OR ("
            "length(oidc_issuer_sha256) = 64 "
            "AND oidc_issuer_sha256 = lower(oidc_issuer_sha256) "
            "AND oidc_issuer_sha256 NOT GLOB '*[^0-9a-f]*')"
        )
    with op.batch_alter_table("users") as batch_op:
        batch_op.create_check_constraint(
            CHECK_NAME,
            check_expression,
        )
    op.create_index(INDEX_NAME, "users", ["oidc_issuer_sha256"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "SELECT pg_catalog.set_config("
                "'search_path', 'pg_catalog,public', true)"
            )
        )
        bind.execute(
            sa.text("SELECT pg_catalog.pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": IDENTITY_BOOTSTRAP_LOCK_ID},
        )
    if bind.dialect.name == "postgresql":
        bound_identity_query = sa.text(
            "SELECT count(*) FROM public.users "
            "WHERE oidc_issuer_sha256 IS NOT NULL"
        )
    else:
        bound_identity_query = sa.text(
            "SELECT count(*) FROM users WHERE oidc_issuer_sha256 IS NOT NULL"
        )
    bound_identity_count = bind.scalar(bound_identity_query)
    if bound_identity_count != 0:
        raise RuntimeError(
            "OIDC_ISSUER_BINDING_DOWNGRADE_BLOCKED: issuer-bound identities "
            "cannot be restored to raw subjects; use an approved pre-binding backup"
        )
    op.drop_index(INDEX_NAME, table_name="users")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint(CHECK_NAME, type_="check")
        batch_op.drop_column("oidc_issuer_sha256")
