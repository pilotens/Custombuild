"""Deterministic, domain-separated workshop challenge derivation.

The database persists only the public derivation context, key version and the
SHA-256 digest of each challenge.  A deployment-owned HMAC key can rederive an
exact response after an uncertain network outcome without persisting the raw
nonce.  Callers must still consume all three digests atomically with chain
acceptance; this module deliberately has no persistence side effects.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from .workshop_trust import STAGE_ORDER, WorkshopStage, canonical_json_bytes

WORKSHOP_NONCE_DERIVATION_SCHEMA_VERSION: Final = (
    "custombuild.workshop-nonce-derivation.v1"
)
WORKSHOP_NONCE_PREFIX: Final = "wn1_"
WORKSHOP_NONCE_CONTEXT_BYTES: Final = 32
MINIMUM_WORKSHOP_NONCE_SECRET_BYTES: Final = 32

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class WorkshopNonceError(ValueError):
    """Raised when a challenge cannot be safely derived or compared."""


@dataclass(frozen=True, slots=True)
class WorkshopNonceChallenge:
    stage: WorkshopStage
    nonce: str = field(repr=False)
    nonce_sha256: str


@dataclass(frozen=True, slots=True)
class WorkshopNonceSet:
    key_version: str
    derivation_context_base64: str
    challenges: tuple[WorkshopNonceChallenge, WorkshopNonceChallenge, WorkshopNonceChallenge]

    def nonce_by_stage(self) -> Mapping[WorkshopStage, str]:
        return {item.stage: item.nonce for item in self.challenges}

    def digest_by_stage(self) -> Mapping[WorkshopStage, str]:
        return {item.stage: item.nonce_sha256 for item in self.challenges}


def new_workshop_nonce_context() -> str:
    """Return a canonical 256-bit public derivation context."""

    return _encode_context(secrets.token_bytes(WORKSHOP_NONCE_CONTEXT_BYTES))


def derive_workshop_nonce_set(
    *,
    secret: bytes,
    key_version: str,
    organization_id: str,
    run_sha256: str,
    nonce_set_id: str,
    generation: int,
    derivation_context_base64: str,
) -> WorkshopNonceSet:
    """Derive all three exact stage challenges from persisted public inputs."""

    _validate_inputs(
        secret=secret,
        key_version=key_version,
        organization_id=organization_id,
        run_sha256=run_sha256,
        nonce_set_id=nonce_set_id,
        generation=generation,
        derivation_context_base64=derivation_context_base64,
    )
    challenges = tuple(
        _derive_stage_nonce(
            secret=secret,
            key_version=key_version,
            organization_id=organization_id,
            run_sha256=run_sha256,
            nonce_set_id=nonce_set_id,
            generation=generation,
            derivation_context_base64=derivation_context_base64,
            stage=stage,
        )
        for stage in STAGE_ORDER
    )
    return WorkshopNonceSet(
        key_version=key_version,
        derivation_context_base64=derivation_context_base64,
        challenges=(challenges[0], challenges[1], challenges[2]),
    )


def workshop_nonce_matches_digest(nonce: object, expected_sha256: object) -> bool:
    """Compare one presented raw challenge to its locked database digest."""

    if (
        type(nonce) is not str
        or not nonce.startswith(WORKSHOP_NONCE_PREFIX)
        or _TOKEN_RE.fullmatch(nonce) is None
        or type(expected_sha256) is not str
        or _SHA256_RE.fullmatch(expected_sha256) is None
    ):
        return False
    actual = hashlib.sha256(nonce.encode("ascii")).hexdigest()
    return hmac.compare_digest(actual, expected_sha256)


def _derive_stage_nonce(
    *,
    secret: bytes,
    key_version: str,
    organization_id: str,
    run_sha256: str,
    nonce_set_id: str,
    generation: int,
    derivation_context_base64: str,
    stage: WorkshopStage,
) -> WorkshopNonceChallenge:
    payload = canonical_json_bytes(
        {
            "schema_version": WORKSHOP_NONCE_DERIVATION_SCHEMA_VERSION,
            "key_version": key_version,
            "organization_id": organization_id,
            "run_sha256": run_sha256,
            "nonce_set_id": nonce_set_id,
            "generation": generation,
            "derivation_context_base64": derivation_context_base64,
            "stage": stage.value,
        }
    )
    material = hmac.digest(secret, payload, "sha256")
    nonce = WORKSHOP_NONCE_PREFIX + base64.urlsafe_b64encode(material).decode().rstrip("=")
    return WorkshopNonceChallenge(
        stage=stage,
        nonce=nonce,
        nonce_sha256=hashlib.sha256(nonce.encode("ascii")).hexdigest(),
    )


def _validate_inputs(
    *,
    secret: bytes,
    key_version: str,
    organization_id: str,
    run_sha256: str,
    nonce_set_id: str,
    generation: int,
    derivation_context_base64: str,
) -> None:
    if type(secret) is not bytes or len(secret) < MINIMUM_WORKSHOP_NONCE_SECRET_BYTES:
        raise WorkshopNonceError("workshop nonce secret must contain at least 256 bits")
    if type(key_version) is not str or _TOKEN_RE.fullmatch(key_version) is None:
        raise WorkshopNonceError("workshop nonce key version is invalid")
    for label, value in (("organization", organization_id), ("nonce set", nonce_set_id)):
        if type(value) is not str or _UUID_RE.fullmatch(value) is None:
            raise WorkshopNonceError(f"workshop nonce {label} is invalid")
    if type(run_sha256) is not str or _SHA256_RE.fullmatch(run_sha256) is None:
        raise WorkshopNonceError("workshop nonce run digest is invalid")
    if type(generation) is not int or generation < 1 or generation > 1_000_000_000:
        raise WorkshopNonceError("workshop nonce generation is invalid")
    _decode_context(derivation_context_base64)


def _encode_context(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_context(value: str) -> bytes:
    if type(value) is not str or len(value) != 43:
        raise WorkshopNonceError("workshop nonce derivation context is invalid")
    try:
        decoded = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise WorkshopNonceError("workshop nonce derivation context is invalid") from exc
    if (
        len(decoded) != WORKSHOP_NONCE_CONTEXT_BYTES
        or _encode_context(decoded) != value
    ):
        raise WorkshopNonceError("workshop nonce derivation context is not canonical")
    return decoded
