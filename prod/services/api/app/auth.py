from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated
from urllib.parse import urlparse

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from .config import get_settings
from .models import Role

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
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("OIDC jwks_uri must use HTTPS")
    return PyJWKClient(jwks_uri, cache_keys=True)


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
        return Principal(
            user_id=str(claims.get("user_id") or claims["sub"]),
            organization_id=str(claims["organization_id"]),
            role=Role(str(claims["role"])),
            subject=str(claims["sub"]),
            email=str(claims.get("email", "")),
            name=str(claims.get("name", claims["sub"])),
        )
    except (jwt.PyJWTError, httpx.HTTPError, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


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


ROLE_RANK: dict[Role, int] = {
    Role.viewer: 0,
    Role.operator: 1,
    Role.designer: 2,
    Role.production: 3,
    Role.reviewer: 4,
    Role.admin: 5,
    Role.owner: 6,
}


def require_minimum_role(minimum: Role) -> Callable[[Principal], Principal]:
    def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if ROLE_RANK[principal.role] < ROLE_RANK[minimum]:
            raise HTTPException(status_code=403, detail=f"Role {minimum.value} or higher required")
        return principal

    return dependency
