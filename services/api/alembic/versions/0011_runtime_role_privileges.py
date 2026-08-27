"""Replace blanket runtime grants with explicit least-privilege access.

Revision ID: 0011_runtime_role_privileges
Revises: 0010_tenant_graph_foreign_keys
"""

from __future__ import annotations

from alembic import op

from scripts.postgres_runtime_privileges import (
    RUNTIME_ROLES,
    runtime_privilege_statements,
)

revision = "0011_runtime_role_privileges"
down_revision = "0010_tenant_graph_foreign_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOINHERIT does not prevent a member from escalating with SET ROLE. Refuse
    # to certify the allow-list if either runtime can assume any other role.
    op.execute(
        """DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM pg_auth_members membership
            JOIN pg_roles member_role ON member_role.oid = membership.member
            WHERE member_role.rolname IN ('custombuild_api', 'custombuild_worker')
          ) THEN
            RAISE EXCEPTION 'runtime database roles must not inherit or assume other roles';
          END IF;
        END $$"""
    )
    for statement in runtime_privilege_statements():
        op.execute(statement)


def downgrade() -> None:
    roles = ", ".join(RUNTIME_ROLES)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {roles}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {roles}")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {roles}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {roles}"
    )
