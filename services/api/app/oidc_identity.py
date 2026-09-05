"""Stable issuer bindings for provisioned OIDC identities."""

from __future__ import annotations

import hashlib

_IDENTITY_KEY_DOMAIN = b"custombuild.oidc-identity.v1\x00"


def oidc_identity_key(issuer: str, subject: str) -> str:
    """Return a collision-safe, non-reversible key for one issuer/subject pair."""

    if not issuer or not subject:
        raise ValueError("OIDC issuer and subject must be non-empty")
    issuer_bytes = issuer.encode("utf-8")
    subject_bytes = subject.encode("utf-8")
    digest = hashlib.sha256()
    digest.update(_IDENTITY_KEY_DOMAIN)
    digest.update(len(issuer_bytes).to_bytes(8, "big"))
    digest.update(issuer_bytes)
    digest.update(len(subject_bytes).to_bytes(8, "big"))
    digest.update(subject_bytes)
    return f"oidc:v1:{digest.hexdigest()}"


def oidc_issuer_sha256(issuer: str) -> str:
    """Return the persistent non-secret binding for one exact OIDC issuer."""

    if not issuer:
        raise ValueError("OIDC issuer must be non-empty")
    return hashlib.sha256(issuer.encode("utf-8")).hexdigest()
