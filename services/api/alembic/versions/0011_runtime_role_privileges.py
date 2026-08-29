"""Replace blanket runtime grants with explicit least-privilege access.

Revision ID: 0011_runtime_role_privileges
Revises: 0010_tenant_graph_foreign_keys
"""

from __future__ import annotations

from alembic import op

revision = "0011_runtime_role_privileges"
down_revision = "0010_tenant_graph_foreign_keys"
branch_labels = None
depends_on = None

# Migration history must never import the mutable current allow-list. A fresh
# database has no storage ledger tables until 0012, so adding their current
# grants here would make 0011 fail before 0012 can create them.
RUNTIME_ROLES = ("custombuild_api", "custombuild_worker")
FROZEN_PRIVILEGE_STATEMENTS = (
    "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
    "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
    "REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC",
    "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
    "REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC",
    "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
    "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC",
    "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
    "REVOKE ALL PRIVILEGES ON TABLES FROM custombuild_api, custombuild_worker",
    "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
    "REVOKE ALL PRIVILEGES ON SEQUENCES FROM custombuild_api, custombuild_worker",
    "ALTER DEFAULT PRIVILEGES FOR ROLE custombuild_migrator IN SCHEMA public "
    "REVOKE EXECUTE ON FUNCTIONS FROM custombuild_api, custombuild_worker",
    "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC",
    "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC",
    "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC",
    "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "
    "custombuild_api, custombuild_worker",
    "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "
    "custombuild_api, custombuild_worker",
    "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM "
    "custombuild_api, custombuild_worker",
    "REVOKE ALL PRIVILEGES ON SCHEMA public FROM custombuild_api, custombuild_worker",
    "GRANT USAGE ON SCHEMA public TO custombuild_api, custombuild_worker",
    "GRANT INSERT ON TABLE audit_events, outbox_events TO custombuild_api",
    "GRANT SELECT ON TABLE memberships, users TO custombuild_api",
    "GRANT SELECT, DELETE ON TABLE artifacts TO custombuild_api",
    "GRANT SELECT, INSERT ON TABLE external_evidence, imported_assets, releases "
    "TO custombuild_api",
    "GRANT SELECT, INSERT, UPDATE ON TABLE design_versions, generation_jobs, projects "
    "TO custombuild_api",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE approvals TO custombuild_api",
    "GRANT INSERT ON TABLE audit_events TO custombuild_worker",
    "GRANT SELECT ON TABLE approvals, design_versions, external_evidence, organizations "
    "TO custombuild_worker",
    "GRANT SELECT, INSERT ON TABLE artifacts TO custombuild_worker",
    "GRANT SELECT, INSERT, UPDATE ON TABLE outbox_events TO custombuild_worker",
    "GRANT SELECT, UPDATE ON TABLE generation_jobs TO custombuild_worker",
)


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
    for statement in FROZEN_PRIVILEGE_STATEMENTS:
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
