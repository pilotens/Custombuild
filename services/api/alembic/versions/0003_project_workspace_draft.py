"""Persist server-authoritative mutable project workspace drafts.

Revision ID: 0003_project_workspace_draft
Revises: 0002_generation_engine_context
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_project_workspace_draft"
down_revision = "0002_generation_engine_context"
branch_labels = None
depends_on = None


def _column_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {str(column["name"]) for column in inspector.get_columns("projects")}


def upgrade() -> None:
    columns = _column_names()
    additions = (
        ("draft_template_id", sa.String(length=80)),
        ("draft_design_hash", sa.String(length=64)),
        ("draft_spec_json", sa.JSON()),
        ("draft_workspace_json", sa.JSON()),
        ("draft_result_json", sa.JSON()),
        ("draft_updated_by", sa.String(length=36)),
    )
    for name, column_type in additions:
        if name not in columns:
            op.add_column("projects", sa.Column(name, column_type, nullable=True))
    if "draft_updated_by" not in columns:
        op.create_foreign_key(
            "fk_projects_draft_updated_by_users",
            "projects",
            "users",
            ["draft_updated_by"],
            ["id"],
        )


def downgrade() -> None:
    columns = _column_names()
    if "draft_updated_by" in columns:
        op.drop_constraint("fk_projects_draft_updated_by_users", "projects", type_="foreignkey")
    for name in (
        "draft_updated_by",
        "draft_result_json",
        "draft_workspace_json",
        "draft_spec_json",
        "draft_design_hash",
        "draft_template_id",
    ):
        if name in columns:
            op.drop_column("projects", name)
