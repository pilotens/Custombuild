from __future__ import annotations

from collections.abc import Iterator

import pytest
from app import auth
from app.db import Base
from app.models import Membership, Organization, Role, User
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ORG_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ORG_ID = "22222222-2222-4222-8222-222222222222"
USER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SUBJECT = "identity-provider-subject-123"


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
                    oidc_sub=SUBJECT,
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
        {"subject": "unknown-subject"},
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
