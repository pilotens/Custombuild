from __future__ import annotations

import os
import uuid
from dataclasses import replace

import pytest
from app.models import Role
from app.oidc_identity import oidc_identity_key, oidc_issuer_sha256
from sqlalchemy import text
from sqlalchemy.orm import Session

from scripts.bootstrap_production_identity import (
    BOOTSTRAP_LOCK_ID,
    BootstrapRequest,
    _engine,
    _guard_database_connection,
    bootstrap_identity,
    provision_additional_member,
)

pytestmark = pytest.mark.postgres


def test_postgres_bootstrap_is_rls_bound_serialized_and_atomic() -> None:
    database_url = os.getenv("MIGRATION_DATABASE_URL")
    if not database_url:
        pytest.skip("MIGRATION_DATABASE_URL is required for the PostgreSQL bootstrap probe")

    suffix = uuid.uuid4().hex
    organization_id = str(uuid.uuid4())
    other_organization_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    request = BootstrapRequest(
        organization_id=organization_id,
        organization_slug=f"identity-bootstrap-{suffix}",
        organization_name=f"Identity bootstrap {suffix}",
        user_id=user_id,
        oidc_issuer="https://identity.integration.example.test",
        oidc_subject=f"bootstrap-subject-{suffix}",
        email=f"bootstrap-{suffix}@example.test",
        user_name=f"Bootstrap owner {suffix}",
        role=Role.owner,
        operator_reference=f"integration-probe-{suffix}",
    )
    designer = replace(
        request,
        user_id=str(uuid.uuid4()),
        oidc_subject=f"bootstrap-designer-{suffix}",
        email=f"bootstrap-designer-{suffix}@example.test",
        user_name=f"Bootstrap designer {suffix}",
        role=Role.designer,
        operator_reference=f"integration-designer-{suffix}",
    )
    reviewer = replace(
        request,
        user_id=str(uuid.uuid4()),
        oidc_subject=f"bootstrap-reviewer-{suffix}",
        email=f"bootstrap-reviewer-{suffix}@example.test",
        user_name=f"Bootstrap reviewer {suffix}",
        role=Role.reviewer,
        operator_reference=f"integration-reviewer-{suffix}",
    )
    cam_reviewer = replace(
        request,
        user_id=str(uuid.uuid4()),
        oidc_subject=f"bootstrap-cam-reviewer-{suffix}",
        email=f"bootstrap-cam-reviewer-{suffix}@example.test",
        user_name=f"Bootstrap CAM reviewer {suffix}",
        role=Role.reviewer,
        operator_reference=f"integration-cam-reviewer-{suffix}",
    )
    engine = _engine(database_url)
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False, autoflush=False)
    try:
        _guard_database_connection(session)
        issuer_column = session.execute(
            text(
                "SELECT data_type, character_maximum_length, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'users' "
                "AND column_name = 'oidc_issuer_sha256'"
            )
        ).one()
        assert issuer_column == ("character varying", 64, "YES")
        issuer_check = session.scalar(
            text(
                "SELECT pg_catalog.pg_get_constraintdef(constraint_row.oid) "
                "FROM pg_catalog.pg_constraint constraint_row "
                "JOIN pg_catalog.pg_class relation "
                "ON relation.oid = constraint_row.conrelid "
                "JOIN pg_catalog.pg_namespace namespace ON namespace.oid = relation.relnamespace "
                "WHERE namespace.nspname = 'public' AND relation.relname = 'users' "
                "AND constraint_row.conname = 'ck_users_oidc_issuer_sha256_format'"
            )
        )
        assert isinstance(issuer_check, str)
        assert "[0-9a-f]{64}" in issuer_check
        created = bootstrap_identity(session, request)
        repeated = bootstrap_identity(session, request)
        assert created.status == "created"
        assert repeated.status == "unchanged"
        assert repeated.audit_event_id == created.audit_event_id
        stored_identity = session.execute(
            text(
                "SELECT oidc_sub, oidc_issuer_sha256 FROM public.users WHERE id = :user_id"
            ),
            {"user_id": user_id},
        ).one()
        assert stored_identity == (
            oidc_identity_key(request.oidc_issuer, request.oidc_subject),
            oidc_issuer_sha256(request.oidc_issuer),
        )
        assert provision_additional_member(session, designer).status == "created"
        assert provision_additional_member(session, reviewer).status == "created"
        assert provision_additional_member(session, cam_reviewer).status == "created"

        with engine.connect() as competing_connection:
            lock_available = competing_connection.scalar(
                text("SELECT pg_catalog.pg_try_advisory_xact_lock(:lock_id)"),
                {"lock_id": BOOTSTRAP_LOCK_ID},
            )
            assert lock_available is False

        session.execute(
            text("SELECT set_config('app.current_organization_id', :tenant, true)"),
            {"tenant": other_organization_id},
        )
        assert session.scalar(
            text(
                "SELECT count(*) FROM public.memberships "
                "WHERE organization_id = :organization_id AND user_id = :user_id"
            ),
            {"organization_id": organization_id, "user_id": user_id},
        ) == 0
        assert session.scalar(
            text(
                "SELECT count(*) FROM public.audit_events "
                "WHERE organization_id = :organization_id AND id = :audit_event_id"
            ),
            {
                "organization_id": organization_id,
                "audit_event_id": created.audit_event_id,
            },
        ) == 0

        session.execute(
            text("SELECT set_config('app.current_organization_id', :tenant, true)"),
            {"tenant": organization_id},
        )
        assert session.scalar(
            text(
                "SELECT count(*) FROM public.memberships "
                "WHERE organization_id = :organization_id AND user_id = :user_id"
            ),
            {"organization_id": organization_id, "user_id": user_id},
        ) == 1
        assert session.scalar(
            text(
                "SELECT count(*) FROM public.memberships "
                "WHERE organization_id = :organization_id"
            ),
            {"organization_id": organization_id},
        ) == 4
        assert session.scalar(
            text(
                "SELECT count(*) FROM public.audit_events "
                "WHERE organization_id = :organization_id AND id = :audit_event_id"
            ),
            {
                "organization_id": organization_id,
                "audit_event_id": created.audit_event_id,
            },
        ) == 1
        assert session.scalar(
            text(
                "SELECT count(*) FROM public.audit_events "
                "WHERE organization_id = :organization_id"
            ),
            {"organization_id": organization_id},
        ) == 4
    finally:
        session.close()
        transaction.rollback()
        connection.close()

    with engine.connect() as verification_connection:
        assert verification_connection.scalar(
            text("SELECT count(*) FROM public.organizations WHERE id = :organization_id"),
            {"organization_id": organization_id},
        ) == 0
        assert verification_connection.scalar(
            text("SELECT count(*) FROM public.users WHERE id = :user_id"),
            {"user_id": user_id},
        ) == 0
    engine.dispose()
