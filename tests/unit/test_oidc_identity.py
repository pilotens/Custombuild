from __future__ import annotations

from collections.abc import Iterator

import pytest
from app import auth
from app.db import Base
from app.models import Membership, Organization, Role, User
from app.oidc_identity import oidc_identity_key, oidc_issuer_sha256
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ORG_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ORG_ID = "22222222-2222-4222-8222-222222222222"
USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SUBJECT = "identity-provider-subject-123"
ISSUER = "https://identity.example.test"


@pytest.fixture
def oidc_identity_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add_all(
            [
                Organization(id=ORG_ID, name="Nordic", slug="nordic"),
                Organization(id=OTHER_ORG_ID, name="Other", slug="other"),
                User(
                    id=USER_ID,
                    oidc_sub=oidc_identity_key(ISSUER, SUBJECT),
                    oidc_issuer_sha256=oidc_issuer_sha256(ISSUER),
                    email="provisioned@example.test",
                    name="Provisioned User",
                ),
                Membership(
                    organization_id=ORG_ID,
                    user_id=USER_ID,
                    role=Role.reviewer,
                ),
            ]
        )
    monkeypatch.setattr(auth, "get_session_factory", lambda: factory)
    monkeypatch.setattr(auth, "set_tenant_context", lambda _session, _organization_id: None)
    yield
    engine.dispose()


def resolve(**overrides: object) -> auth.Principal:
    arguments: dict[str, object] = {
        "issuer": ISSUER,
        "subject": SUBJECT,
        "organization_id": ORG_ID,
        "claimed_role": Role.reviewer,
        "claimed_user_id": None,
        "email": "",
        "name": "",
    }
    arguments.update(overrides)
    return auth._resolve_oidc_principal(**arguments)  # type: ignore[arg-type]


def test_signed_subject_resolves_to_internal_user_and_membership(
    oidc_identity_store: None,
) -> None:
    principal = resolve()

    assert principal.user_id == USER_ID
    assert principal.subject == SUBJECT
    assert principal.organization_id == ORG_ID
    assert principal.role == Role.reviewer
    assert principal.email == "provisioned@example.test"
    assert principal.name == "Provisioned User"


def test_identity_key_is_private_and_unambiguous() -> None:
    key = oidc_identity_key(ISSUER, SUBJECT)

    assert key.startswith("oidc:v1:")
    assert ISSUER not in key
    assert SUBJECT not in key
    assert oidc_identity_key("https://example.test/ab", "c") != oidc_identity_key(
        "https://example.test/a", "bc"
    )


def test_optional_user_id_must_be_bound_to_the_same_subject(
    oidc_identity_store: None,
) -> None:
    assert resolve(claimed_user_id=USER_ID).user_id == USER_ID

    with pytest.raises(HTTPException) as caught:
        resolve(claimed_user_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

    assert caught.value.status_code == 403
    assert caught.value.detail == "Active organization membership required"


@pytest.mark.parametrize(
    ("overrides"),
    [
        {"issuer": "https://replacement-issuer.example.test"},
        {"issuer": ""},
        {"subject": "unknown-subject"},
        {"subject": ""},
        {"organization_id": OTHER_ORG_ID},
        {"claimed_role": Role.owner},
    ],
)
def test_unknown_subject_wrong_organization_or_role_is_denied(
    oidc_identity_store: None,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(HTTPException) as caught:
        resolve(**overrides)

    assert caught.value.status_code == 403
    assert caught.value.detail == "Active organization membership required"


def test_legacy_raw_subject_fails_with_explicit_binding_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add_all(
            [
                Organization(id=ORG_ID, name="Nordic", slug="nordic"),
                User(
                    id=USER_ID,
                    oidc_sub=SUBJECT,
                    email="legacy@example.test",
                    name="Legacy User",
                ),
                Membership(
                    organization_id=ORG_ID,
                    user_id=USER_ID,
                    role=Role.reviewer,
                ),
            ]
        )
    monkeypatch.setattr(auth, "get_session_factory", lambda: factory)
    monkeypatch.setattr(auth, "set_tenant_context", lambda _session, _organization_id: None)
    try:
        with pytest.raises(HTTPException) as caught:
            resolve()
    finally:
        engine.dispose()

    assert caught.value.status_code == 503
    assert caught.value.detail == "OIDC identity requires explicit issuer binding"


def test_premarker_opaque_identity_fails_with_explicit_binding_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add_all(
            [
                Organization(id=ORG_ID, name="Nordic", slug="nordic"),
                User(
                    id=USER_ID,
                    oidc_sub=oidc_identity_key(ISSUER, SUBJECT),
                    email="legacy@example.test",
                    name="Legacy User",
                ),
                Membership(
                    organization_id=ORG_ID,
                    user_id=USER_ID,
                    role=Role.reviewer,
                ),
            ]
        )
    monkeypatch.setattr(auth, "get_session_factory", lambda: factory)
    monkeypatch.setattr(auth, "set_tenant_context", lambda _session, _organization_id: None)
    try:
        with pytest.raises(HTTPException) as caught:
            resolve()
    finally:
        engine.dispose()

    assert caught.value.status_code == 503
    assert caught.value.detail == "OIDC identity requires explicit issuer binding"
