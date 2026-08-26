from __future__ import annotations

import importlib
import os
import uuid
from collections.abc import Mapping

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError


def _assert_foreign_key_violation(
    engine: Engine,
    statement: str,
    parameters: Mapping[str, object],
    constraint_name: str,
) -> None:
    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(IntegrityError) as caught:
            connection.execute(text(statement), parameters)
        transaction.rollback()

    diagnostic = getattr(caught.value.orig, "diag", None)
    actual_constraint = getattr(diagnostic, "constraint_name", None)
    assert actual_constraint == constraint_name


@pytest.mark.postgres
def test_real_tables_enforce_rls_for_non_superuser() -> None:
    tenant_graph_url = os.getenv("TENANT_GRAPH_DATABASE_URL")
    api_url = os.getenv("RLS_DATABASE_URL")
    if not tenant_graph_url or not api_url:
        pytest.skip("PostgreSQL RLS probe requires fixture-admin and API database URLs")

    suffix = uuid.uuid4().hex[:10]
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    project_a = str(uuid.uuid4())
    project_b = str(uuid.uuid4())
    user_a = str(uuid.uuid4())
    user_b = str(uuid.uuid4())
    import_a = str(uuid.uuid4())
    import_b = str(uuid.uuid4())
    fixture_admin = create_engine(tenant_graph_url)
    with fixture_admin.begin() as connection:
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
                "INSERT INTO users (id, oidc_sub, email, name, created_at, updated_at) "
                "VALUES (:id, :oidc_sub, :email, :name, now(), now())"
            ),
            [
                {
                    "id": user_a,
                    "oidc_sub": f"rls-user-a-{suffix}",
                    "email": f"rls-a-{suffix}@example.test",
                    "name": "RLS user A",
                },
                {
                    "id": user_b,
                    "oidc_sub": f"rls-user-b-{suffix}",
                    "email": f"rls-b-{suffix}@example.test",
                    "name": "RLS user B",
                },
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
        connection.execute(
            text(
                "INSERT INTO imported_assets "
                "(id, organization_id, project_id, sha256, object_key, size_bytes, "
                "media_type, original_filename, created_by, created_at, updated_at) VALUES "
                "(:id, :organization_id, :project_id, :sha256, :object_key, 12, "
                "'image/png', 'reference.png', :created_by, now(), now())"
            ),
            [
                {
                    "id": import_a,
                    "organization_id": org_a,
                    "project_id": project_a,
                    "sha256": "a" * 64,
                    "object_key": f"private/{suffix}/a",
                    "created_by": user_a,
                },
                {
                    "id": import_b,
                    "organization_id": org_b,
                    "project_id": project_b,
                    "sha256": "b" * 64,
                    "object_key": f"private/{suffix}/b",
                    "created_by": user_b,
                },
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
        updated_revision = connection.execute(
            text(
                "UPDATE projects SET draft_revision = draft_revision + 1 "
                "WHERE id = :id RETURNING draft_revision"
            ),
            {"id": project_a},
        ).scalar_one()
        assert updated_revision == 1
        hidden_update = connection.execute(
            text(
                "UPDATE projects SET draft_revision = draft_revision + 1 "
                "WHERE id = :id RETURNING draft_revision"
            ),
            {"id": project_b},
        ).scalar_one_or_none()
        assert hidden_update is None
        visible_imports = connection.execute(
            text("SELECT id FROM imported_assets WHERE id IN (:a, :b) ORDER BY id"),
            {"a": import_a, "b": import_b},
        ).scalars().all()
        assert visible_imports == [import_a]


@pytest.mark.postgres
def test_rls_rejects_cross_tenant_insert() -> None:
    tenant_graph_url = os.getenv("TENANT_GRAPH_DATABASE_URL")
    api_url = os.getenv("RLS_DATABASE_URL")
    if not tenant_graph_url or not api_url:
        pytest.skip("PostgreSQL RLS probe is only run in CI")
    own_org = str(uuid.uuid4())
    other_org = str(uuid.uuid4())
    suffix = uuid.uuid4().hex[:10]
    fixture_admin = create_engine(tenant_graph_url)
    with fixture_admin.begin() as connection:
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


@pytest.mark.postgres
def test_nobypass_migrator_preflight_rejects_legacy_cross_tenant_row_before_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_graph_url = os.getenv("TENANT_GRAPH_DATABASE_URL")
    migration_url = os.getenv("MIGRATION_DATABASE_URL")
    if not tenant_graph_url or not migration_url:
        pytest.skip("Tenant preflight probe requires bootstrap and migrator database URLs")

    migration = importlib.import_module(
        "services.api.alembic.versions.0010_tenant_graph_foreign_keys"
    )
    suffix = uuid.uuid4().hex[:10]
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    project_a = str(uuid.uuid4())
    mismatched_design = str(uuid.uuid4())
    privileged_engine = create_engine(tenant_graph_url)
    migrator_engine = create_engine(migration_url)

    try:
        with privileged_engine.begin() as connection:
            bootstrap_role = connection.execute(
                text(
                    "SELECT current_user, rolsuper FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            ).one()
            assert bootstrap_role.rolsuper, (
                "TENANT_GRAPH_DATABASE_URL must use the explicit bootstrap superuser "
                "to seed a pre-0010 legacy row"
            )
            connection.execute(
                text(
                    "INSERT INTO organizations (id, name, slug, created_at, updated_at) "
                    "VALUES (:id, :name, :slug, now(), now())"
                ),
                [
                    {"id": org_a, "name": "Preflight parent", "slug": f"preflight-a-{suffix}"},
                    {"id": org_b, "name": "Preflight child", "slug": f"preflight-b-{suffix}"},
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO projects "
                    "(id, organization_id, name, description, furniture_type, "
                    "current_revision, archived, created_at, updated_at) VALUES "
                    "(:id, :organization_id, 'Legacy parent', '', 'bookcase', "
                    "0, false, now(), now())"
                ),
                {"id": project_a, "organization_id": org_a},
            )
            # The current schema already has 0010. Disable FK triggers only for
            # this bootstrap insert to reproduce a row possible before 0010.
            connection.execute(text("SET LOCAL session_replication_role = replica"))
            connection.execute(
                text(
                    "INSERT INTO designs "
                    "(id, organization_id, project_id, name, created_at, updated_at) "
                    "VALUES (:id, :organization_id, :project_id, "
                    "'Legacy cross-tenant child', now(), now())"
                ),
                {
                    "id": mismatched_design,
                    "organization_id": org_b,
                    "project_id": project_a,
                },
            )
            connection.execute(text("SET LOCAL session_replication_role = origin"))

        with migrator_engine.begin() as connection:
            migrator_role = connection.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            ).one()
            assert migrator_role == ("custombuild_migrator", False, False)
            table_names = {
                "organizations",
                *(
                    relationship.child_table
                    for relationship in migration.TENANT_RELATIONSHIPS
                ),
                *(
                    relationship.parent_table
                    for relationship in migration.TENANT_RELATIONSHIPS
                ),
            }
            for table_name in sorted(table_names):
                owner, can_select = connection.execute(
                    text(
                        "SELECT pg_get_userbyid(relowner), "
                        "has_table_privilege(current_user, oid, 'SELECT') "
                        "FROM pg_class WHERE oid = to_regclass(:table_name)"
                    ),
                    {"table_name": f"public.{table_name}"},
                ).one()
                assert owner == "custombuild_migrator"
                assert can_select is True

            monkeypatch.setattr(migration.op, "get_bind", lambda: connection)

            def unexpected_ddl(*_args: object, **_kwargs: object) -> None:
                raise AssertionError("tenant graph DDL ran before the preflight passed")

            monkeypatch.setattr(
                migration.op,
                "create_unique_constraint",
                unexpected_ddl,
            )
            monkeypatch.setattr(
                migration,
                "_replace_with_tenant_foreign_key",
                unexpected_ddl,
            )
            with pytest.raises(
                RuntimeError,
                match=(
                    r"TENANT_GRAPH_PRECHECK_FAILED: .*"
                    r"designs\.project_id -> projects\.id has 1 conflicting row"
                ),
            ):
                migration.upgrade()
            assert (
                connection.execute(
                    text(
                        "SELECT current_setting("
                        "'app.current_organization_id', true)"
                    )
                ).scalar_one()
                == ""
            )
    finally:
        with privileged_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM designs WHERE id = :id"),
                {"id": mismatched_design},
            )
            connection.execute(
                text("DELETE FROM organizations WHERE id IN (:org_a, :org_b)"),
                {"org_a": org_a, "org_b": org_b},
            )
        migrator_engine.dispose()
        privileged_engine.dispose()


@pytest.mark.postgres
def test_tenant_foreign_keys_reject_cross_tenant_children_for_bypassrls_session() -> None:
    tenant_graph_url = os.getenv("TENANT_GRAPH_DATABASE_URL")
    if not tenant_graph_url:
        pytest.skip("Tenant graph probe requires TENANT_GRAPH_DATABASE_URL")

    suffix = uuid.uuid4().hex[:10]
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    user_a = str(uuid.uuid4())
    user_b = str(uuid.uuid4())
    project_a = str(uuid.uuid4())
    project_b = str(uuid.uuid4())
    version_a = str(uuid.uuid4())
    version_b = str(uuid.uuid4())
    job_a = str(uuid.uuid4())
    job_b = str(uuid.uuid4())
    privileged_engine = create_engine(tenant_graph_url)

    with privileged_engine.begin() as connection:
        role = connection.execute(
            text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
        ).one()
        assert role.rolsuper or role.rolbypassrls, (
            "TENANT_GRAPH_DATABASE_URL must use an explicit superuser or BYPASSRLS probe role"
        )

        connection.execute(
            text(
                "INSERT INTO organizations (id, name, slug, created_at, updated_at) "
                "VALUES (:id, :name, :slug, now(), now())"
            ),
            [
                {"id": org_a, "name": "FK tenant A", "slug": f"fk-a-{suffix}"},
                {"id": org_b, "name": "FK tenant B", "slug": f"fk-b-{suffix}"},
            ],
        )
        connection.execute(
            text(
                "INSERT INTO users (id, oidc_sub, email, name, created_at, updated_at) "
                "VALUES (:id, :oidc_sub, :email, :name, now(), now())"
            ),
            [
                {
                    "id": user_a,
                    "oidc_sub": f"fk-user-a-{suffix}",
                    "email": f"fk-a-{suffix}@example.test",
                    "name": "FK user A",
                },
                {
                    "id": user_b,
                    "oidc_sub": f"fk-user-b-{suffix}",
                    "email": f"fk-b-{suffix}@example.test",
                    "name": "FK user B",
                },
            ],
        )
        connection.execute(
            text(
                "INSERT INTO projects "
                "(id, organization_id, name, description, furniture_type, current_revision, "
                "archived, created_at, updated_at) VALUES "
                "(:id, :organization_id, :name, '', 'bookcase', 1, false, now(), now())"
            ),
            [
                {"id": project_a, "organization_id": org_a, "name": "FK project A"},
                {"id": project_b, "organization_id": org_b, "name": "FK project B"},
            ],
        )
        connection.execute(
            text(
                "INSERT INTO design_versions "
                "(id, organization_id, project_id, revision, status, design_hash, "
                "context_hash, spec_json, source_provenance_json, result_json, "
                "engine_version, template_version, rule_version, created_by, immutable, "
                "created_at, updated_at) VALUES "
                "(:id, :organization_id, :project_id, 1, 'design_validated', :design_hash, "
                ":context_hash, '{}', '{}', '{}', '1.0.0', 'bookcase@1.0.0', '1.0.0', "
                ":created_by, true, now(), now())"
            ),
            [
                {
                    "id": version_a,
                    "organization_id": org_a,
                    "project_id": project_a,
                    "design_hash": "a" * 64,
                    "context_hash": "b" * 64,
                    "created_by": user_a,
                },
                {
                    "id": version_b,
                    "organization_id": org_b,
                    "project_id": project_b,
                    "design_hash": "c" * 64,
                    "context_hash": "d" * 64,
                    "created_by": user_b,
                },
            ],
        )
        connection.execute(
            text(
                "INSERT INTO generation_jobs "
                "(id, organization_id, design_version_id, status, idempotency_key, "
                "production_context_hash, production_engine_context_json, request_json, "
                "result_json, attempts, created_at, updated_at) VALUES "
                "(:id, :organization_id, :design_version_id, 'succeeded', :idempotency_key, "
                ":context_hash, '{}', '{}', '{}', 1, now(), now())"
            ),
            [
                {
                    "id": job_a,
                    "organization_id": org_a,
                    "design_version_id": version_a,
                    "idempotency_key": f"fk-job-a-{suffix}",
                    "context_hash": "e" * 64,
                },
                {
                    "id": job_b,
                    "organization_id": org_b,
                    "design_version_id": version_b,
                    "idempotency_key": f"fk-job-b-{suffix}",
                    "context_hash": "f" * 64,
                },
            ],
        )

    project_child_attempts = (
        (
            "INSERT INTO designs "
            "(id, organization_id, project_id, name, created_at, updated_at) "
            "VALUES (:id, :organization_id, :project_id, 'Denied', now(), now())",
            {
                "id": str(uuid.uuid4()),
                "organization_id": org_b,
                "project_id": project_a,
            },
            "fk_designs_org_project",
        ),
        (
            "INSERT INTO imported_assets "
            "(id, organization_id, project_id, sha256, object_key, size_bytes, media_type, "
            "original_filename, created_by, created_at, updated_at) VALUES "
            "(:id, :organization_id, :project_id, :sha256, :object_key, 1, 'image/png', "
            "'denied.png', :created_by, now(), now())",
            {
                "id": str(uuid.uuid4()),
                "organization_id": org_b,
                "project_id": project_a,
                "sha256": uuid.uuid4().hex * 2,
                "object_key": f"private/{suffix}/denied-import",
                "created_by": user_b,
            },
            "fk_imported_assets_org_project",
        ),
        (
            "INSERT INTO external_evidence "
            "(id, organization_id, project_id, evidence_type, rule_id, catalog_id, "
            "catalog_version, design_hash, object_key, sha256, size_bytes, content_type, "
            "created_by, created_at, updated_at) VALUES "
            "(:id, :organization_id, :project_id, 'engineering', 'FK-001', 'catalog', "
            "'1', :design_hash, :object_key, :sha256, 1, 'application/pdf', :created_by, "
            "now(), now())",
            {
                "id": str(uuid.uuid4()),
                "organization_id": org_b,
                "project_id": project_a,
                "design_hash": "1" * 64,
                "object_key": f"private/{suffix}/denied-evidence",
                "sha256": "2" * 64,
                "created_by": user_b,
            },
            "fk_external_evidence_org_project",
        ),
        (
            "INSERT INTO design_versions "
            "(id, organization_id, project_id, revision, status, design_hash, context_hash, "
            "spec_json, source_provenance_json, result_json, engine_version, "
            "template_version, rule_version, created_by, immutable, created_at, updated_at) "
            "VALUES (:id, :organization_id, :project_id, 99, 'draft', :design_hash, "
            ":context_hash, '{}', '{}', '{}', '1.0.0', 'bookcase@1.0.0', '1.0.0', "
            ":created_by, false, now(), now())",
            {
                "id": str(uuid.uuid4()),
                "organization_id": org_b,
                "project_id": project_a,
                "design_hash": "3" * 64,
                "context_hash": "4" * 64,
                "created_by": user_b,
            },
            "fk_design_versions_org_project",
        ),
    )
    for statement, parameters, constraint_name in project_child_attempts:
        _assert_foreign_key_violation(
            privileged_engine,
            statement,
            parameters,
            constraint_name,
        )

    version_child_attempts = [
        (
            "INSERT INTO generation_jobs "
            "(id, organization_id, design_version_id, status, idempotency_key, "
            "production_context_hash, production_engine_context_json, request_json, "
            "attempts, created_at, updated_at) VALUES "
            "(:id, :organization_id, :design_version_id, 'queued', :idempotency_key, "
            ":context_hash, '{}', '{}', 0, now(), now())",
            {
                "id": str(uuid.uuid4()),
                "organization_id": org_b,
                "design_version_id": version_a,
                "idempotency_key": f"fk-denied-job-{suffix}",
                "context_hash": "5" * 64,
            },
            "fk_generation_jobs_org_design_version",
        ),
        (
            "INSERT INTO releases "
            "(id, organization_id, design_version_id, release_number, released_by, "
            "manifest_sha256, created_at, updated_at) VALUES "
            "(:id, :organization_id, :design_version_id, 'denied', :released_by, "
            ":manifest_sha256, now(), now())",
            {
                "id": str(uuid.uuid4()),
                "organization_id": org_b,
                "design_version_id": version_a,
                "released_by": user_b,
                "manifest_sha256": "6" * 64,
            },
            "fk_releases_org_design_version",
        ),
        (
            "INSERT INTO approvals "
            "(id, organization_id, design_version_id, approval_type, approved_by, reason, "
            "overrides_json, created_at, updated_at) VALUES "
            "(:id, :organization_id, :design_version_id, 'design', :approved_by, "
            "'Denied', '[]', now(), now())",
            {
                "id": str(uuid.uuid4()),
                "organization_id": org_b,
                "design_version_id": version_a,
                "approved_by": user_b,
            },
            "fk_approvals_org_design_version",
        ),
    ]
    production_tables = (
        "assembly_graphs",
        "assembly_steps",
        "bom_lines",
        "cam_operations",
        "constraints",
        "cut_list_lines",
        "load_cases",
        "manufacturing_features",
        "nesting_layouts",
        "parameter_definitions",
        "parameter_values",
        "part_faces",
        "parts",
        "rule_evaluations",
        "setups",
        "toolpaths",
    )
    version_child_attempts.extend(
        (
            f"INSERT INTO {table_name} "  # noqa: S608
            "(id, organization_id, design_version_id, stable_key, data_json, "
            "created_at, updated_at) VALUES "
            "(:id, :organization_id, :design_version_id, :stable_key, '{}', now(), now())",
            {
                "id": str(uuid.uuid4()),
                "organization_id": org_b,
                "design_version_id": version_a,
                "stable_key": f"denied-{suffix}-{table_name}",
            },
            f"fk_{table_name}_org_design_version",
        )
        for table_name in production_tables
    )
    for statement, parameters, constraint_name in version_child_attempts:
        _assert_foreign_key_violation(
            privileged_engine,
            statement,
            parameters,
            constraint_name,
        )

    _assert_foreign_key_violation(
        privileged_engine,
        "INSERT INTO artifacts "
        "(id, organization_id, generation_job_id, kind, object_key, sha256, size_bytes, "
        "content_type, created_at, updated_at) VALUES "
        "(:id, :organization_id, :generation_job_id, 'denied', :object_key, :sha256, 1, "
        "'application/octet-stream', now(), now())",
        {
            "id": str(uuid.uuid4()),
            "organization_id": org_b,
            "generation_job_id": job_a,
            "object_key": f"private/{suffix}/denied-artifact",
            "sha256": "7" * 64,
        },
        "fk_artifacts_org_generation_job",
    )
    _assert_foreign_key_violation(
        privileged_engine,
        "INSERT INTO approvals "
        "(id, organization_id, design_version_id, approval_type, approved_by, reason, "
        "generation_job_id, production_context_hash, manifest_sha256, overrides_json, "
        "created_at, updated_at) VALUES "
        "(:id, :organization_id, :design_version_id, 'cam', :approved_by, 'Denied', "
        ":generation_job_id, :context_hash, :manifest_sha256, '[]', now(), now())",
        {
            "id": str(uuid.uuid4()),
            "organization_id": org_b,
            "design_version_id": version_b,
            "approved_by": user_b,
            "generation_job_id": job_a,
            "context_hash": "8" * 64,
            "manifest_sha256": "9" * 64,
        },
        "fk_approvals_org_generation_job",
    )
