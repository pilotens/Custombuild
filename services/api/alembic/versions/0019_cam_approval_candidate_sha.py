"""Bind CAM approvals to an exact executable candidate bundle digest.

Revision ID: 0019_cam_approval_candidate_sha
Revises: 0018_joint_retention_registry_state

Existing approvals remain NULL deliberately.  A pre-migration approval did not
cryptographically attest this field, so an executable candidate must be
reviewed again before it can pass the release freshness check.  Jobs without a
cutting candidate continue to use NULL as their exact binding.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_cam_approval_candidate_sha"
down_revision = "0018_joint_retention_registry_state"
branch_labels = None
depends_on = None

CHECK_NAME = "ck_approvals_cam_candidate_bundle_sha256_format"


def _bound_approval_count(bind: sa.Connection) -> int:
    """Count protected approval rows without bypassing PostgreSQL FORCE RLS."""

    if bind.dialect.name != "postgresql":
        return int(
            bind.scalar(
                sa.text(
                    "SELECT count(*) FROM approvals WHERE cam_candidate_bundle_sha256 IS NOT NULL"
                )
            )
            or 0
        )

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
            count += int(
                bind.scalar(
                    sa.text(
                        "SELECT count(*) FROM public.approvals "
                        "WHERE cam_candidate_bundle_sha256 IS NOT NULL"
                    )
                )
                or 0
            )
    finally:
        bind.execute(sa.text("SELECT set_config('app.current_organization_id', '', true)"))
    return count


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text("SELECT pg_catalog.set_config('search_path', 'pg_catalog,public', true)")
        )
    op.add_column(
        "approvals",
        sa.Column(
            "cam_candidate_bundle_sha256",
            sa.String(length=64),
            nullable=True,
        ),
    )
    if bind.dialect.name == "postgresql":
        expression = (
            "cam_candidate_bundle_sha256 IS NULL OR cam_candidate_bundle_sha256 ~ '^[0-9a-f]{64}$'"
        )
    else:
        expression = (
            "cam_candidate_bundle_sha256 IS NULL OR ("
            "length(cam_candidate_bundle_sha256) = 64 "
            "AND cam_candidate_bundle_sha256 = lower(cam_candidate_bundle_sha256) "
            "AND cam_candidate_bundle_sha256 NOT GLOB '*[^0-9a-f]*')"
        )
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.create_check_constraint(CHECK_NAME, expression)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text("SELECT pg_catalog.set_config('search_path', 'pg_catalog,public', true)")
        )
    if _bound_approval_count(bind) != 0:
        raise RuntimeError(
            "CAM_APPROVAL_CANDIDATE_BINDING_DOWNGRADE_BLOCKED: exact executable "
            "candidate approval identities would be lost; restore an approved backup"
        )
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.drop_constraint(CHECK_NAME, type_="check")
        batch_op.drop_column("cam_candidate_bundle_sha256")
