"""Allow tenant-scoped furniture history and committed export status reads.

Revision ID: 0021_furniture_review_reads
Revises: 0020_release_cam_approval_identity

Existing FORCE RLS policies remain in effect. The API cannot update/delete
either audit history or outbox delivery state.
"""

from __future__ import annotations

from alembic import op

revision = "0021_furniture_review_reads"
down_revision = "0020_release_cam_approval_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("GRANT SELECT ON TABLE audit_events, outbox_events TO custombuild_api")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("REVOKE SELECT ON TABLE audit_events, outbox_events FROM custombuild_api")
