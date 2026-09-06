"""Freeze the CAM approval identity on executable releases.

Revision ID: 0020_release_cam_approval_identity
Revises: 0019_cam_approval_candidate_sha

Historical design-review releases remain NULL because they never attested an
executable candidate. New executable releases must populate the identity and
the API validates it against the immutable release binding.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_release_cam_approval_identity"
down_revision = "0019_cam_approval_candidate_sha"
branch_labels = None
depends_on = None

UUID_CHECK_NAME = "ck_releases_cam_approval_id_format"
RECEIPT_CHECK_NAME = "ck_releases_cam_approval_receipt_complete"
APPROVAL_UNIQUE_NAME = "uq_approvals_org_version_job_id"
APPROVAL_FK_NAME = "fk_releases_org_version_job_cam_approval"


def _portable_lower_hex_sql(expression: str) -> str:
    """Return SQL accepted with identical semantics by SQLite and PostgreSQL."""

    for character in "0123456789abcdef":
        expression = f"replace({expression}, '{character}', '')"
    return f"{expression} = ''"


UUID_CHECK_SQL = (
    "cam_approval_id IS NULL OR (length(cam_approval_id) = 36 "
    "AND cam_approval_id = lower(cam_approval_id) "
    "AND substr(cam_approval_id, 9, 1) = '-' "
    "AND substr(cam_approval_id, 14, 1) = '-' "
    "AND substr(cam_approval_id, 19, 1) = '-' "
    "AND substr(cam_approval_id, 24, 1) = '-' "
    "AND length(replace(cam_approval_id, '-', '')) = 32 "
    f"AND {_portable_lower_hex_sql("replace(cam_approval_id, '-', '')")} "
    "AND substr(cam_approval_id, 15, 1) IN ('1', '2', '3', '4', '5') "
    "AND substr(cam_approval_id, 20, 1) IN ('8', '9', 'a', 'b'))"
)
RECEIPT_CHECK_SQL = (
    "(cam_approval_id IS NULL AND cam_approval_binding_sha256 IS NULL "
    "AND cam_approval_snapshot_json IS NULL) OR "
    "(cam_approval_id IS NOT NULL AND cam_approval_binding_sha256 IS NOT NULL "
    "AND length(cam_approval_binding_sha256) = 64 "
    "AND cam_approval_binding_sha256 = lower(cam_approval_binding_sha256) "
    f"AND {_portable_lower_hex_sql('cam_approval_binding_sha256')} "
    "AND cam_approval_snapshot_json IS NOT NULL)"
)
_SQLITE_RECEIPT_COUNT_SQL = (
    "SELECT count(*) FROM releases "
    "WHERE cam_approval_id IS NOT NULL "
    "OR cam_approval_binding_sha256 IS NOT NULL "
    "OR cam_approval_snapshot_json IS NOT NULL"
)
_POSTGRES_RECEIPT_COUNT_SQL = (
    "SELECT count(*) FROM public.releases "
    "WHERE cam_approval_id IS NOT NULL "
    "OR cam_approval_binding_sha256 IS NOT NULL "
    "OR cam_approval_snapshot_json IS NOT NULL"
)


def _receipt_release_count(bind: sa.Connection) -> int:
    """Count protected release receipts without bypassing PostgreSQL FORCE RLS."""

    if bind.dialect.name != "postgresql":
        return int(bind.scalar(sa.text(_SQLITE_RECEIPT_COUNT_SQL)) or 0)

    organization_ids = tuple(
        str(value)
        for value in bind.execute(sa.text("SELECT id FROM organizations ORDER BY id")).scalars()
    )
    count = 0
    try:
        for organization_id in organization_ids:
            bind.execute(
                sa.text("SELECT set_config('app.current_organization_id', :organization_id, true)"),
                {"organization_id": organization_id},
            )
            count += int(bind.scalar(sa.text(_POSTGRES_RECEIPT_COUNT_SQL)) or 0)
    finally:
        bind.execute(sa.text("SELECT set_config('app.current_organization_id', '', true)"))
    return count


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text("SELECT pg_catalog.set_config('search_path', 'pg_catalog,public', true)")
        )
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.create_unique_constraint(
            APPROVAL_UNIQUE_NAME,
            ["organization_id", "design_version_id", "generation_job_id", "id"],
        )
    op.add_column("releases", sa.Column("cam_approval_id", sa.String(36), nullable=True))
    op.add_column(
        "releases",
        sa.Column("cam_approval_binding_sha256", sa.String(64), nullable=True),
    )
    op.add_column(
        "releases",
        sa.Column(
            "cam_approval_snapshot_json",
            sa.JSON(none_as_null=True),
            nullable=True,
        ),
    )
    with op.batch_alter_table("releases") as batch_op:
        batch_op.create_check_constraint(
            UUID_CHECK_NAME,
            UUID_CHECK_SQL,
        )
        batch_op.create_check_constraint(
            RECEIPT_CHECK_NAME,
            RECEIPT_CHECK_SQL,
        )
        batch_op.create_foreign_key(
            APPROVAL_FK_NAME,
            "approvals",
            ["organization_id", "design_version_id", "generation_job_id", "cam_approval_id"],
            ["organization_id", "design_version_id", "generation_job_id", "id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text("SELECT pg_catalog.set_config('search_path', 'pg_catalog,public', true)")
        )
    if _receipt_release_count(bind) != 0:
        raise RuntimeError(
            "EXECUTABLE_RELEASE_RECEIPT_DOWNGRADE_BLOCKED: immutable CAM approval "
            "identities would be lost; restore an approved backup"
        )
    with op.batch_alter_table("releases") as batch_op:
        batch_op.drop_constraint(APPROVAL_FK_NAME, type_="foreignkey")
        batch_op.drop_constraint(RECEIPT_CHECK_NAME, type_="check")
        batch_op.drop_constraint(UUID_CHECK_NAME, type_="check")
        batch_op.drop_column("cam_approval_snapshot_json")
        batch_op.drop_column("cam_approval_binding_sha256")
        batch_op.drop_column("cam_approval_id")
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.drop_constraint(APPROVAL_UNIQUE_NAME, type_="unique")
