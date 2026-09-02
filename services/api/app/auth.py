from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from types import MappingProxyType
from typing import Annotated, Final
from urllib.parse import urlparse

import httpx2 as httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from sqlalchemy import select

from .config import get_settings
from .db import get_session_factory, set_tenant_context
from .models import Membership, Role, User

DEV_ORG_NORDIC = "11111111-1111-4111-8111-111111111111"
DEV_ORG_ATELIER = "22222222-2222-4222-8222-222222222222"
DEV_USER_NORDIC = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DEV_USER_ATELIER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    organization_id: str
    role: Role
    subject: str
    email: str
    name: str


_DEV_TOKENS: dict[str, Principal] = {
    "demo-nordic-owner": Principal(
        user_id=DEV_USER_NORDIC,
        organization_id=DEV_ORG_NORDIC,
        role=Role.owner,
        subject="demo:nordic-owner",
        email="owner@nordic.example",
        name="Nordic Demo Owner",
    ),
    "demo-atelier-owner": Principal(
        user_id=DEV_USER_ATELIER,
        organization_id=DEV_ORG_ATELIER,
        role=Role.owner,
        subject="demo:atelier-owner",
        email="owner@atelier.example",
        name="Atelier Demo Owner",
    ),
}

bearer = HTTPBearer(auto_error=False)


class Capability(StrEnum):
    """One independently grantable API action.

    Capabilities deliberately describe actions rather than a role hierarchy.  In
    particular, workshop preparation, execution, verification and revocation
    are separate so a single operational identity cannot silently inherit a
    maker/checker or administrative action.
    """

    READ = "read"
    DESIGN = "design"
    GENERATE = "generate"
    REVIEW = "review"
    PRODUCTION_RELEASE = "production_release"
    WORKSHOP_PREPARE = "workshop_prepare"
    WORKSHOP_CHALLENGE = "workshop_challenge"
    WORKSHOP_EVIDENCE = "workshop_evidence"
    WORKSHOP_ATTEST = "workshop_attest"
    WORKSHOP_VERIFY = "workshop_verify"
    WORKSHOP_REVOKE = "workshop_revoke"
    ADMINISTER = "administer"


_ALL_CAPABILITIES: Final[frozenset[Capability]] = frozenset(Capability)

# This is the authorization policy, not an ordering.  Every non-administrative
# role has a closed, reviewable allow-list.  Owner and admin intentionally have
# full access; their distinction remains an organization-governance concern.
ROLE_CAPABILITIES: Final[Mapping[Role, frozenset[Capability]]] = MappingProxyType(
    {
        Role.viewer: frozenset({Capability.READ}),
        Role.operator: frozenset(
            {
                Capability.READ,
                Capability.WORKSHOP_CHALLENGE,
                Capability.WORKSHOP_EVIDENCE,
                Capability.WORKSHOP_ATTEST,
            }
        ),
        Role.designer: frozenset(
            {
                Capability.READ,
                Capability.DESIGN,
                Capability.GENERATE,
            }
        ),
        Role.production: frozenset(
            {
                Capability.READ,
                Capability.PRODUCTION_RELEASE,
                Capability.WORKSHOP_PREPARE,
                Capability.WORKSHOP_CHALLENGE,
                Capability.WORKSHOP_EVIDENCE,
                Capability.WORKSHOP_ATTEST,
            }
        ),
        Role.reviewer: frozenset(
            {
                Capability.READ,
                Capability.REVIEW,
                Capability.WORKSHOP_VERIFY,
            }
        ),
        Role.admin: _ALL_CAPABILITIES,
        Role.owner: _ALL_CAPABILITIES,
    }
)


@lru_cache(maxsize=1)
def _jwk_client() -> PyJWKClient:
    issuer = get_settings().oidc_issuer.rstrip("/")
    response = httpx.get(
        f"{issuer}/.well-known/openid-configuration",
        timeout=5,
        follow_redirects=False,
    )
    response.raise_for_status()
    discovery = response.json()
    if str(discovery.get("issuer", "")).rstrip("/") != issuer:
        raise ValueError("OIDC discovery issuer mismatch")
    jwks_uri = str(discovery.get("jwks_uri", ""))
    parsed = urlparse(jwks_uri)
    issuer_url = urlparse(issuer)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.scheme, parsed.hostname, parsed.port)
        != (issuer_url.scheme, issuer_url.hostname, issuer_url.port)
    ):
        raise ValueError("OIDC jwks_uri must use the configured issuer origin")
    return PyJWKClient(jwks_uri, cache_keys=True)


def _resolve_oidc_principal(
    *,
    subject: str,
    organization_id: str,
    claimed_role: Role,
    claimed_user_id: str | None,
    email: str,
    name: str,
) -> Principal:
    """Bind signed OIDC claims to one provisioned internal identity and membership."""

    factory = get_session_factory()
    with factory.begin() as session:
        set_tenant_context(session, organization_id)
        user = session.scalar(select(User).where(User.oidc_sub == subject))
        if user is None or (claimed_user_id is not None and claimed_user_id != user.id):
            raise HTTPException(status_code=403, detail="Active organization membership required")
        membership = session.scalar(
            select(Membership).where(
                Membership.organization_id == organization_id,
                Membership.user_id == user.id,
            )
        )
        if membership is None or membership.role != claimed_role:
            raise HTTPException(status_code=403, detail="Active organization membership required")
        return Principal(
            user_id=user.id,
            organization_id=organization_id,
            role=membership.role,
            subject=subject,
            email=email or user.email,
            name=name or user.name,
        )


def _oidc_principal(token: str) -> Principal:
    settings = get_settings()
    try:
        signing_key = _jwk_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iat", "sub", "organization_id", "role"]},
        )
        subject = str(claims["sub"])
        organization_id = str(claims["organization_id"])
        claimed_role = Role(str(claims["role"]))
        raw_user_id = claims.get("user_id")
        claimed_user_id = str(raw_user_id) if raw_user_id is not None else None
        email = str(claims.get("email", ""))
        name = str(claims.get("name", ""))
    except (jwt.PyJWTError, httpx.HTTPError, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return _resolve_oidc_principal(
        subject=subject,
        organization_id=organization_id,
        claimed_role=claimed_role,
        claimed_user_id=claimed_user_id,
        email=email,
        name=name,
    )


def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    settings = get_settings()
    if settings.auth_mode == "development":
        principal = _DEV_TOKENS.get(credentials.credentials)
        if principal is None:
            raise HTTPException(status_code=401, detail="Unknown development token")
        return principal
    return _oidc_principal(credentials.credentials)


def capabilities_for_role(role: Role) -> frozenset[Capability]:
    """Return the immutable capability set assigned to ``role``."""

    return ROLE_CAPABILITIES[role]


def require_capability(capability: Capability) -> Callable[[Principal], Principal]:
    """Create a FastAPI dependency which enforces one explicit capability."""

    def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if capability not in capabilities_for_role(principal.role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Capability {capability.value} required",
            )
        return principal

    return dependency
