from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from app.db import Base
from app.models import AuditEvent, Membership, Organization, Role, User
from app.oidc_identity import oidc_identity_key, oidc_issuer_sha256
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from scripts import bootstrap_production_identity as bootstrap

ORGANIZATION_ID = "3ef2d58c-1afe-4f26-a96c-d8dbdcf24a14"
USER_ID = "fd822611-0d47-44c2-ac9e-a04c7601b34a"
OTHER_USER_ID = "8c2eb1e8-942c-4f89-b5de-ced70f3b7c30"
OTHER_ORGANIZATION_ID = "4ce9f4d7-7756-4785-9eb6-2d179c24f9f4"
OIDC_ISSUER = "https://identity.example.test/realms/custombuild"
OIDC_SUBJECT = "00u-production-owner"


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Organization.__table__,
            User.__table__,
            Membership.__table__,
            AuditEvent.__table__,
        ],
    )
    yield sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    engine.dispose()


def request(**overrides: object) -> bootstrap.BootstrapRequest:
    values: dict[str, object] = {
        "organization_id": ORGANIZATION_ID,
        "organization_slug": "nordic-production",
        "organization_name": "Nordic Production",
        "user_id": USER_ID,
        "oidc_issuer": OIDC_ISSUER,
        "oidc_subject": OIDC_SUBJECT,
        "email": "owner@example.test",
        "user_name": "Production Owner",
        "role": Role.owner,
        "operator_reference": "change-request-CB-1042",
    }
    values.update(overrides)
    return bootstrap.BootstrapRequest(**values)  # type: ignore[arg-type]


def request_payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_version": bootstrap.BOOTSTRAP_REQUEST_SCHEMA_VERSION,
        "organization_id": ORGANIZATION_ID,
        "organization_slug": "nordic-production",
        "organization_name": "Nordic Production",
        "user_id": USER_ID,
        "oidc_issuer": OIDC_ISSUER,
        "oidc_subject": OIDC_SUBJECT,
        "email": "owner@example.test",
        "user_name": "Production Owner",
        "role": "owner",
        "operator_reference": "change-request-CB-1042",
    }
    values.update(overrides)
    return values


def test_bootstrap_creates_exact_identity_membership_and_pii_safe_audit(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        result = bootstrap.bootstrap_identity(session, request())

    assert result.status == "created"
    with factory() as session:
        organization = session.get(Organization, ORGANIZATION_ID)
        user = session.get(User, USER_ID)
        membership = session.scalar(
            select(Membership).where(
                Membership.organization_id == ORGANIZATION_ID,
                Membership.user_id == USER_ID,
            )
        )
        audit = session.get(AuditEvent, result.audit_event_id)
        assert organization is not None
        assert (organization.slug, organization.name) == (
            "nordic-production",
            "Nordic Production",
        )
        assert user is not None
        assert (user.oidc_sub, user.email, user.name) == (
            oidc_identity_key(OIDC_ISSUER, OIDC_SUBJECT),
            "owner@example.test",
            "Production Owner",
        )
        assert user.oidc_issuer_sha256 == oidc_issuer_sha256(OIDC_ISSUER)
        assert membership is not None and membership.role is Role.owner
        assert audit is not None
        assert audit.actor_id == USER_ID
        assert audit.action == bootstrap.BOOTSTRAP_AUDIT_ACTION
        assert audit.payload_json == {
            "schema_version": bootstrap.BOOTSTRAP_AUDIT_SCHEMA_VERSION,
            "organization_slug": "nordic-production",
            "organization_name_sha256": bootstrap._digest("Nordic Production"),
            "role": "owner",
            "oidc_issuer_sha256": bootstrap._digest(OIDC_ISSUER),
            "oidc_subject_sha256": bootstrap._digest(OIDC_SUBJECT),
            "email_sha256": bootstrap._digest("owner@example.test"),
            "user_name_sha256": bootstrap._digest("Production Owner"),
            "performed_by": "external_operator_via_production_bootstrap_cli",
            "operator_reference_sha256": bootstrap._digest("change-request-CB-1042"),
            "actor_id_semantics": "provisioned_target_identity_not_external_operator",
            "operation": bootstrap.BOOTSTRAP_AUDIT_ACTION,
        }
        serialized_audit = json.dumps(audit.payload_json)
        assert OIDC_ISSUER not in serialized_audit
        assert OIDC_SUBJECT not in serialized_audit
        assert "owner@example.test" not in serialized_audit
        assert "change-request-CB-1042" not in serialized_audit
        assert "Nordic Production" not in serialized_audit
        assert "Production Owner" not in serialized_audit


def test_identical_retry_is_a_noop_even_after_other_members_are_added(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        first = bootstrap.bootstrap_identity(session, request())
    with factory.begin() as session:
        session.add(
            User(
                id=OTHER_USER_ID,
                oidc_sub="later-admin",
                email="later@example.test",
                name="Later Admin",
            )
        )
        session.add(
            Membership(
                organization_id=ORGANIZATION_ID,
                user_id=OTHER_USER_ID,
                role=Role.admin,
            )
        )
    with factory.begin() as session:
        second = bootstrap.bootstrap_identity(session, request())

    assert first.audit_event_id == second.audit_event_id
    assert second.status == "unchanged"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Organization)) == 1
        assert session.scalar(select(func.count()).select_from(User)) == 2
        assert session.scalar(select(func.count()).select_from(Membership)) == 2
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_designer_and_two_reviewers_are_distinct_and_idempotent(
    factory: sessionmaker[Session],
) -> None:
    designer = request(
        user_id=OTHER_USER_ID,
        oidc_subject="production-designer",
        email="designer@example.test",
        user_name="Production Designer",
        role=Role.designer,
        operator_reference="change-request-CB-1044",
    )
    reviewer_id = "db5945ee-738d-4f30-a715-eca3f82f98d4"
    reviewer = request(
        user_id=reviewer_id,
        oidc_subject="production-reviewer",
        email="reviewer@example.test",
        user_name="Production Reviewer",
        role=Role.reviewer,
        operator_reference="change-request-CB-1045",
    )
    cam_reviewer_id = "c57cf550-ce62-4539-a92c-159b8ef0777d"
    cam_reviewer = request(
        user_id=cam_reviewer_id,
        oidc_subject="production-cam-reviewer",
        email="cam-reviewer@example.test",
        user_name="Production CAM Reviewer",
        role=Role.reviewer,
        operator_reference="change-request-CB-1046",
    )
    with factory.begin() as session:
        bootstrap.bootstrap_identity(session, request())
    with factory.begin() as session:
        assert bootstrap.provision_additional_member(session, designer).status == "created"
        assert bootstrap.provision_additional_member(session, reviewer).status == "created"
        assert bootstrap.provision_additional_member(session, cam_reviewer).status == "created"
    with factory.begin() as session:
        assert bootstrap.provision_additional_member(session, designer).status == "unchanged"
        assert bootstrap.provision_additional_member(session, reviewer).status == "unchanged"
        assert bootstrap.provision_additional_member(session, cam_reviewer).status == "unchanged"

    with factory() as session:
        memberships = {
            membership.user_id: membership.role
            for membership in session.scalars(select(Membership)).all()
        }
        assert memberships == {
            USER_ID: Role.owner,
            OTHER_USER_ID: Role.designer,
            reviewer_id: Role.reviewer,
            cam_reviewer_id: Role.reviewer,
        }
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 4


def test_additional_member_requires_existing_admin_and_non_admin_role(
    factory: sessionmaker[Session],
) -> None:
    designer = request(
        user_id=OTHER_USER_ID,
        oidc_subject="production-designer",
        email="designer@example.test",
        user_name="Production Designer",
        role=Role.designer,
    )
    with pytest.raises(
        bootstrap.IdentityBootstrapError,
        match="exact bootstrapped",
    ), factory.begin() as session:
        bootstrap.provision_additional_member(session, designer)
    with factory.begin() as session:
        bootstrap.bootstrap_identity(session, request())
    with pytest.raises(
        bootstrap.IdentityBootstrapError,
        match="designer or reviewer",
    ), factory.begin() as session:
        bootstrap.provision_additional_member(session, request(role=Role.owner))


def test_additional_member_requires_admin_bound_to_the_same_issuer(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        bootstrap.bootstrap_identity(session, request())

    different_issuer_member = request(
        user_id=OTHER_USER_ID,
        oidc_issuer="https://replacement-identity.example.test",
        oidc_subject="replacement-issuer-designer",
        email="designer@example.test",
        user_name="Production Designer",
        role=Role.designer,
    )
    with pytest.raises(
        bootstrap.IdentityBootstrapError,
        match="identity bound to another OIDC issuer",
    ), factory.begin() as session:
        bootstrap.provision_additional_member(session, different_issuer_member)

    with factory() as session:
        assert session.get(User, OTHER_USER_ID) is None


def test_bootstrap_cannot_create_a_mixed_issuer_database(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        bootstrap.bootstrap_identity(session, request())

    other_tenant = request(
        organization_id=OTHER_ORGANIZATION_ID,
        organization_slug="other-production",
        organization_name="Other Production",
        user_id=OTHER_USER_ID,
        oidc_issuer="https://replacement-identity.example.test",
        oidc_subject="replacement-issuer-owner",
        email="other-owner@example.test",
        user_name="Other Production Owner",
        operator_reference="change-request-CB-2042",
    )
    with pytest.raises(
        bootstrap.IdentityBootstrapError,
        match="identity bound to another OIDC issuer",
    ), factory.begin() as session:
        bootstrap.bootstrap_identity(session, other_tenant)

    with factory() as session:
        assert session.get(Organization, OTHER_ORGANIZATION_ID) is None
        assert session.get(User, OTHER_USER_ID) is None


def test_explicit_legacy_binding_is_audited_and_idempotent(
    factory: sessionmaker[Session],
) -> None:
    legacy = request(role=Role.reviewer)
    with factory.begin() as session:
        session.add_all(
            [
                Organization(
                    id=ORGANIZATION_ID,
                    slug="nordic-production",
                    name="Nordic Production",
                ),
                User(
                    id=USER_ID,
                    oidc_sub=OIDC_SUBJECT,
                    email="owner@example.test",
                    name="Production Owner",
                ),
                Membership(
                    organization_id=ORGANIZATION_ID,
                    user_id=USER_ID,
                    role=Role.reviewer,
                ),
            ]
        )

    with factory.begin() as session:
        result = bootstrap.bind_legacy_oidc_identity(session, legacy)
        assert result.status == "bound"
    with factory.begin() as session:
        repeated = bootstrap.bind_legacy_oidc_identity(session, legacy)
        assert repeated.status == "unchanged"
        assert repeated.audit_event_id == result.audit_event_id
    with factory() as session:
        user = session.get(User, USER_ID)
        audit = session.get(AuditEvent, result.audit_event_id)
        assert user is not None
        assert user.oidc_sub == oidc_identity_key(OIDC_ISSUER, OIDC_SUBJECT)
        assert user.oidc_issuer_sha256 == oidc_issuer_sha256(OIDC_ISSUER)
        assert audit is not None
        assert audit.action == bootstrap.LEGACY_BINDING_AUDIT_ACTION
        assert audit.payload_json["operation"] == bootstrap.LEGACY_BINDING_AUDIT_ACTION


def test_explicit_binding_accepts_an_exact_premarker_opaque_identity(
    factory: sessionmaker[Session],
) -> None:
    legacy = request(role=Role.reviewer)
    opaque_key = oidc_identity_key(OIDC_ISSUER, OIDC_SUBJECT)
    with factory.begin() as session:
        session.add_all(
            [
                Organization(
                    id=ORGANIZATION_ID,
                    slug="nordic-production",
                    name="Nordic Production",
                ),
                User(
                    id=USER_ID,
                    oidc_sub=opaque_key,
                    email="owner@example.test",
                    name="Production Owner",
                ),
                Membership(
                    organization_id=ORGANIZATION_ID,
                    user_id=USER_ID,
                    role=Role.reviewer,
                ),
            ]
        )

    with factory.begin() as session:
        result = bootstrap.bind_legacy_oidc_identity(session, legacy)

    assert result.status == "bound"
    with factory() as session:
        user = session.get(User, USER_ID)
        assert user is not None
        assert user.oidc_sub == opaque_key
        assert user.oidc_issuer_sha256 == oidc_issuer_sha256(OIDC_ISSUER)


def test_legacy_binding_refuses_global_user_with_multiple_tenant_memberships(
    factory: sessionmaker[Session],
) -> None:
    legacy = request(role=Role.reviewer)
    with factory.begin() as session:
        session.add_all(
            [
                Organization(
                    id=ORGANIZATION_ID,
                    slug="nordic-production",
                    name="Nordic Production",
                ),
                Organization(
                    id=OTHER_ORGANIZATION_ID,
                    slug="other-production",
                    name="Other Production",
                ),
                User(
                    id=USER_ID,
                    oidc_sub=OIDC_SUBJECT,
                    email="owner@example.test",
                    name="Production Owner",
                ),
                Membership(
                    organization_id=ORGANIZATION_ID,
                    user_id=USER_ID,
                    role=Role.reviewer,
                ),
                Membership(
                    organization_id=OTHER_ORGANIZATION_ID,
                    user_id=USER_ID,
                    role=Role.viewer,
                ),
            ]
        )

    with pytest.raises(
        bootstrap.IdentityBootstrapError,
        match="exactly one matching tenant membership",
    ), factory.begin() as session:
        bootstrap.bind_legacy_oidc_identity(session, legacy)

    with factory() as session:
        user = session.get(User, USER_ID)
        assert user is not None and user.oidc_sub == OIDC_SUBJECT
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_role_or_identity_drift_is_never_silently_applied(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        bootstrap.bootstrap_identity(session, request())

    for changed in (
        request(role=Role.admin),
        request(organization_name="Renamed Tenant"),
        request(email="different@example.test"),
        request(oidc_subject="replacement-subject"),
        request(operator_reference="change-request-CB-1043"),
    ):
        with pytest.raises(bootstrap.IdentityBootstrapError), factory.begin() as session:
            bootstrap.bootstrap_identity(session, changed)

    with factory() as session:
        membership = session.scalar(select(Membership))
        user = session.get(User, USER_ID)
        organization = session.get(Organization, ORGANIZATION_ID)
        assert membership is not None and membership.role is Role.owner
        assert user is not None
        assert user.oidc_sub == oidc_identity_key(OIDC_ISSUER, OIDC_SUBJECT)
        assert organization is not None and organization.name == "Nordic Production"


@pytest.mark.parametrize(
    ("model_target", "changed_value", "request_overrides"),
    (
        (
            "organization_name",
            "Mutated Organization",
            {"organization_name": "Mutated Organization"},
        ),
        (
            "email",
            "mutated-owner@example.test",
            {"email": "mutated-owner@example.test"},
        ),
        (
            "name",
            "Mutated Owner",
            {"user_name": "Mutated Owner"},
        ),
    ),
)
def test_audit_binding_detects_out_of_band_identity_mutation(
    factory: sessionmaker[Session],
    model_target: str,
    changed_value: str,
    request_overrides: dict[str, object],
) -> None:
    with factory.begin() as session:
        bootstrap.bootstrap_identity(session, request())
    with factory.begin() as session:
        if model_target == "organization_name":
            organization = session.get(Organization, ORGANIZATION_ID)
            assert organization is not None
            organization.name = changed_value
        else:
            user = session.get(User, USER_ID)
            assert user is not None
            setattr(user, model_target, changed_value)

    with pytest.raises(
        bootstrap.IdentityBootstrapError,
        match="audit identity does not match",
    ), factory.begin() as session:
        bootstrap.bootstrap_identity(session, request(**request_overrides))


def test_refuses_to_add_initial_admin_to_an_already_provisioned_organization(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        session.add(
            Organization(
                id=ORGANIZATION_ID,
                slug="nordic-production",
                name="Nordic Production",
            )
        )
        session.add(
            User(
                id=OTHER_USER_ID,
                oidc_sub="existing-owner",
                email="existing@example.test",
                name="Existing Owner",
            )
        )
        session.add(
            Membership(
                organization_id=ORGANIZATION_ID,
                user_id=OTHER_USER_ID,
                role=Role.owner,
            )
        )

    with pytest.raises(
        bootstrap.IdentityBootstrapError,
        match="partial bootstrap identity state",
    ), factory.begin() as session:
        bootstrap.bootstrap_identity(session, request())

    with factory() as session:
        assert session.get(User, USER_ID) is None
        assert session.scalar(select(func.count()).select_from(Membership)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_refuses_preexisting_membership_without_exact_bootstrap_audit(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        session.add(
            Organization(
                id=ORGANIZATION_ID,
                slug="nordic-production",
                name="Nordic Production",
            )
        )
        session.add(
            User(
                id=USER_ID,
                oidc_sub=oidc_identity_key(OIDC_ISSUER, OIDC_SUBJECT),
                oidc_issuer_sha256=oidc_issuer_sha256(OIDC_ISSUER),
                email="owner@example.test",
                name="Production Owner",
            )
        )
        session.add(
            Membership(
                organization_id=ORGANIZATION_ID,
                user_id=USER_ID,
                role=Role.owner,
            )
        )

    with pytest.raises(
        bootstrap.IdentityBootstrapError,
        match="partial bootstrap identity state",
    ), factory.begin() as session:
        bootstrap.bootstrap_identity(session, request())

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"organization_id": str(uuid.uuid4()).upper()}, "canonical UUID"),
        ({"organization_slug": "Nordic Production"}, "organization slug"),
        ({"organization_name": " Nordic"}, "organization name"),
        ({"user_name": "Owner\nInjected"}, "control characters"),
        ({"oidc_issuer": "http://identity.example.test"}, "canonical HTTPS"),
        ({"oidc_subject": ""}, "OIDC subject"),
        ({"email": "Owner@Example.test"}, "canonical lowercase"),
        ({"role": Role.designer}, "owner or admin"),
        ({"operator_reference": "__REPLACE_OPERATOR_REFERENCE__"}, "placeholder"),
    ),
)
def test_request_validation_fails_closed(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(bootstrap.IdentityBootstrapError, match=message):
        bootstrap.validate_request(request(**overrides))


def test_runtime_requires_production_oidc_and_migrator_database() -> None:
    valid = {
        "APP_ENV": "production",
        "AUTH_MODE": "oidc",
        "OIDC_ISSUER": OIDC_ISSUER,
        "DATABASE_URL": (
            "postgresql+psycopg://custombuild_migrator:"
            "strong-production-migrator-password@postgres/custombuild"
        ),
    }
    assert bootstrap._production_database_url(valid) == valid["DATABASE_URL"]
    for override in (
        {"APP_ENV": "development"},
        {"AUTH_MODE": "development"},
        {"DATABASE_URL": ""},
        {
            "DATABASE_URL": (
                "postgresql+psycopg://custombuild_api:"
                "strong-production-api-password@postgres/custombuild"
            )
        },
    ):
        with pytest.raises(bootstrap.IdentityBootstrapError):
            bootstrap._production_database_url({**valid, **override})


def guarded_session(
    *,
    role_overrides: dict[str, Any] | None = None,
    relation_overrides: dict[str, tuple[str, bool, bool]] | None = None,
    policy_expression: str = (
        "((organization_id)::text = "
        "current_setting('app.current_organization_id'::text, true))"
    ),
    membership_count: int = 0,
    revisions: tuple[str, ...] = ("head",),
) -> Session:
    role = {
        "rolname": "custombuild_migrator",
        "rolsuper": False,
        "rolcreaterole": False,
        "rolcreatedb": False,
        "rolinherit": False,
        "rolreplication": False,
        "rolbypassrls": False,
        "active_schemas": ["pg_catalog", "public"],
        **(role_overrides or {}),
    }
    role_result = Mock()
    role_result.mappings.return_value.one_or_none.return_value = role
    relations = {
        "alembic_version": ("custombuild_migrator", False, False),
        "organizations": ("custombuild_migrator", False, False),
        "users": ("custombuild_migrator", False, False),
        "memberships": ("custombuild_migrator", True, True),
        "audit_events": ("custombuild_migrator", True, True),
        **(relation_overrides or {}),
    }
    relations_result = Mock()
    relations_result.tuples.return_value = [
        (name, *settings) for name, settings in relations.items()
    ]
    policies_result = Mock()
    policies_result.tuples.return_value = [
        (
            table_name,
            "tenant_isolation",
            True,
            "*",
            True,
            policy_expression,
            policy_expression,
        )
        for table_name in ("audit_events", "memberships")
    ]
    membership_result = Mock()
    membership_result.scalar_one.return_value = membership_count
    lock_result = Mock()
    revision_result = Mock()
    revision_result.scalars.return_value = iter(revisions)
    session = Mock(spec=Session)
    session.execute.side_effect = (
        role_result,
        relations_result,
        policies_result,
        membership_result,
        lock_result,
        revision_result,
    )
    return cast(Session, session)


def test_database_guard_requires_least_privilege_role_and_exact_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap, "expected_schema_head", lambda: "head")
    session = guarded_session()

    bootstrap._guard_database_connection(session)

    assert cast(Mock, session).execute.call_count == 6


@pytest.mark.parametrize(
    ("session", "message"),
    (
        (guarded_session(role_overrides={"rolname": "custombuild_api"}), "not the production"),
        (guarded_session(role_overrides={"rolsuper": True}), "over-privileged"),
        (guarded_session(role_overrides={"rolbypassrls": True}), "over-privileged"),
        (
            guarded_session(role_overrides={"active_schemas": ["public"]}),
            "search path",
        ),
        (
            guarded_session(relation_overrides={"users": ("postgres", False, False)}),
            "missing, misowned or lack forced RLS",
        ),
        (
            guarded_session(
                relation_overrides={
                    "memberships": ("custombuild_migrator", False, False)
                }
            ),
            "lack forced RLS",
        ),
        (
            guarded_session(policy_expression="true"),
            "exact tenant RLS policies",
        ),
        (guarded_session(membership_count=1), "has role memberships"),
        (guarded_session(revisions=("old-head",)), "not at the repository"),
    ),
)
def test_database_guard_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    message: str,
) -> None:
    monkeypatch.setattr(bootstrap, "expected_schema_head", lambda: "head")
    with pytest.raises(bootstrap.IdentityBootstrapError, match=message):
        bootstrap._guard_database_connection(session)


def test_requested_issuer_must_equal_runtime_issuer() -> None:
    configured = {"OIDC_ISSUER": OIDC_ISSUER}
    assert bootstrap._request_from_payload(request_payload(), configured).oidc_issuer == OIDC_ISSUER
    with pytest.raises(bootstrap.IdentityBootstrapError, match="does not match"):
        bootstrap._request_from_payload(
            request_payload(),
            {"OIDC_ISSUER": "https://other-identity.example.test"},
        )
    with pytest.raises(bootstrap.IdentityBootstrapError, match="does not match"):
        bootstrap._request_from_payload(
            request_payload(),
            {"OIDC_ISSUER": f"{OIDC_ISSUER}/"},
        )


def test_request_file_requires_private_regular_file_and_rejects_duplicate_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bootstrap.json"
    path.write_text(json.dumps(request_payload()), encoding="utf-8")
    path.chmod(0o600)
    assert bootstrap._load_request_file(str(path)) == request_payload()

    path.chmod(0o644)
    with pytest.raises(bootstrap.IdentityBootstrapError, match="mode 0400 or 0600"):
        bootstrap._load_request_file(str(path))

    path.chmod(0o600)
    path.write_text('{"schema_version":"first","schema_version":"second"}', encoding="utf-8")
    with pytest.raises(bootstrap.IdentityBootstrapError, match="duplicate JSON keys"):
        bootstrap._load_request_file(str(path))


def test_cli_never_prints_database_exception_or_identity_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = [
        "--request-file",
        "/run/custombuild/initial-identity.json",
        "--confirm-initial-admin",
    ]
    environment = {
        "APP_ENV": "production",
        "AUTH_MODE": "oidc",
        "OIDC_ISSUER": OIDC_ISSUER,
        "DATABASE_URL": (
            "postgresql+psycopg://custombuild_migrator:"
            "strong-production-migrator-password@postgres/custombuild"
        ),
    }

    def unsafe_engine(_database_url: str) -> None:
        raise OperationalError(
            "connect owner@example.test 00u-production-owner",
            None,
            RuntimeError("strong-production-migrator-password"),
        )

    monkeypatch.setattr(bootstrap, "_load_request_file", lambda _path: request_payload())
    monkeypatch.setattr(bootstrap, "_engine", unsafe_engine)
    assert bootstrap.main(arguments, environment=environment) == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        "production identity bootstrap failed: database operation failed\n"
    )
