from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError


@pytest.mark.postgres
def test_real_tables_enforce_rls_for_non_superuser() -> None:
    migration_url = os.getenv("MIGRATION_DATABASE_URL")
    api_url = os.getenv("RLS_DATABASE_URL")
    if not migration_url or not api_url:
        pytest.skip("PostgreSQL RLS probe is only run when CI provides both database URLs")

    suffix = uuid.uuid4().hex[:10]
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    project_a = str(uuid.uuid4())
    project_b = str(uuid.uuid4())
    migrator = create_engine(migration_url)
    with migrator.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO organizations (id, name, slug, created_at, updated_at) "
                "VALUES (:id, :name, :slug, now(), now())"
            ),
            [
                {"id": org_a, "name": "RLS A", "slug": f"rls-a-{suffix}"},
                {"id": org_b, "name": "RLS B", "slug": f"rls-b-{suffix}"},
            ],
        )
        connection.execute(
            text(
                "INSERT INTO projects "
                "(id, organization_id, name, description, furniture_type, current_revision, "
                "archived, created_at, updated_at) VALUES "
                "(:id, :organization_id, :name, '', 'bookcase', 0, false, now(), now())"
            ),
            [
                {"id": project_a, "organization_id": org_a, "name": "Only A"},
                {"id": project_b, "organization_id": org_b, "name": "Only B"},
            ],
        )

    api_engine = create_engine(api_url)
    with api_engine.begin() as connection:
        role = connection.execute(
            text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
        ).one()
        assert role == (False, False)
        connection.execute(
            text("SELECT set_config('app.current_organization_id', :tenant, true)"),
            {"tenant": org_a},
        )
        visible = connection.execute(
            text("SELECT id FROM projects WHERE id IN (:a, :b) ORDER BY id"),
            {"a": project_a, "b": project_b},
        ).scalars().all()
        assert visible == [project_a]


@pytest.mark.postgres
def test_rls_rejects_cross_tenant_insert() -> None:
    migration_url = os.getenv("MIGRATION_DATABASE_URL")
    api_url = os.getenv("RLS_DATABASE_URL")
    if not migration_url or not api_url:
        pytest.skip("PostgreSQL RLS probe is only run in CI")
    own_org = str(uuid.uuid4())
    other_org = str(uuid.uuid4())
    suffix = uuid.uuid4().hex[:10]
    migrator = create_engine(migration_url)
    with migrator.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO organizations (id, name, slug, created_at, updated_at) "
                "VALUES (:id, :name, :slug, now(), now())"
            ),
            [
                {"id": own_org, "name": "RLS own", "slug": f"rls-own-{suffix}"},
                {"id": other_org, "name": "RLS other", "slug": f"rls-other-{suffix}"},
            ],
        )

    api_engine = create_engine(api_url)
    with api_engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(
            text("SELECT set_config('app.current_organization_id', :tenant, true)"),
            {"tenant": own_org},
        )
        with pytest.raises(DBAPIError, match="row-level security"):
            connection.execute(
                text(
                    "INSERT INTO projects "
                    "(id, organization_id, name, description, furniture_type, current_revision, "
                    "archived, created_at, updated_at) VALUES "
                    "(:id, :organization_id, 'Denied', '', 'bookcase', 0, false, now(), now())"
                ),
                {"id": str(uuid.uuid4()), "organization_id": other_org},
            )
        transaction.rollback()
