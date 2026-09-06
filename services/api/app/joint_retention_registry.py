"""Durable activation boundary for the joint-retention trust registry.

The signed registry document remains the portable source of trust policy.  In
production, PostgreSQL additionally holds a monotonic high-water copy.  API
and worker transactions may use a registry only when its exact canonical bytes
and SHA-256 digest match that activated copy.  Development never writes or
pretends to activate this production state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .joint_retention import (
    JointRetentionTrustError,
    TrustRegistry,
    _canonical_json_bytes,
    _decode_base64,
    _load_registry,
)

MAX_TRUST_REGISTRY_BYTES = 262_144
ASSERT_REGISTRY_FUNCTION = "public.custombuild_joint_retention_assert_registry"


class JointRetentionRegistryError(RuntimeError):
    """The configured registry cannot satisfy the production high-water mark."""


@dataclass(frozen=True, slots=True)
class JointRetentionRegistryBinding:
    registry: TrustRegistry
    canonical_json: str
    sha256: str


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise JointRetentionRegistryError(
                "joint-retention trust registry contains duplicate JSON keys"
            )
        result[key] = value
    return result


def parse_joint_retention_registry_json(encoded: str) -> Mapping[str, Any]:
    """Parse one bounded UTF-8-equivalent JSON object without duplicate keys."""

    try:
        payload_size = len(encoded.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise JointRetentionRegistryError(
            "joint-retention trust registry is not valid UTF-8 text"
        ) from exc
    if payload_size < 1 or payload_size > MAX_TRUST_REGISTRY_BYTES:
        raise JointRetentionRegistryError(
            "joint-retention trust registry is empty or exceeds its size limit"
        )
    try:
        value = json.loads(encoded, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise JointRetentionRegistryError(
            "joint-retention trust registry must contain strict JSON"
        ) from exc
    if not isinstance(value, Mapping):
        raise JointRetentionRegistryError(
            "joint-retention trust registry must contain a JSON object"
        )
    return value


def joint_retention_registry_binding(
    value: Mapping[str, Any],
) -> JointRetentionRegistryBinding:
    """Validate the authoritative schema and bind the exact canonical bytes."""

    try:
        registry = _load_registry(value)
        for issuer in registry.issuers:
            public_key = _decode_base64(
                issuer.public_key_base64,
                label="issuer public key",
                max_bytes=32,
            )
            if len(public_key) != 32:
                raise JointRetentionTrustError("issuer public key must be 32-byte Ed25519 material")
        canonical_bytes = _canonical_json_bytes(value)
        canonical_json = canonical_bytes.decode("utf-8")
    except (JointRetentionTrustError, UnicodeError) as exc:
        raise JointRetentionRegistryError("joint-retention trust registry is invalid") from exc
    return JointRetentionRegistryBinding(
        registry=registry,
        canonical_json=canonical_json,
        sha256=hashlib.sha256(canonical_bytes).hexdigest(),
    )


def validate_monotonic_registry_transition(
    previous: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    """Return whether a valid candidate advances, and reject every rollback.

    Existing issuer identities and validity material are immutable.  A key may
    be revoked or have its revocation moved earlier, but it can never be
    cleared or delayed.  Equivalent timezone-offset spellings compare as the
    same instant; because activation binds exact JSON bytes, changing only that
    spelling still creates a new epoch.  Revocation sets may only grow and
    additional issuer keys may be appended.  ``False`` denotes an exact
    idempotent replay.
    """

    previous_binding = joint_retention_registry_binding(previous)
    candidate_binding = joint_retention_registry_binding(candidate)
    if previous_binding.canonical_json == candidate_binding.canonical_json:
        return False

    previous_raw_issuers = previous.get("issuers")
    candidate_raw_issuers = candidate.get("issuers")
    if not isinstance(previous_raw_issuers, list) or not isinstance(candidate_raw_issuers, list):
        # Authoritative schema validation above guards this.  Keep the
        # transition check fail-closed if its representation ever changes.
        raise JointRetentionRegistryError("registry issuer representation is invalid")
    candidate_issuers = {
        (str(item["issuer_id"]), str(item["key_id"])): item
        for item in candidate_raw_issuers
        if isinstance(item, Mapping)
    }
    previous_parsed_issuers = {
        (item.issuer_id, item.key_id): item for item in previous_binding.registry.issuers
    }
    candidate_parsed_issuers = {
        (item.issuer_id, item.key_id): item for item in candidate_binding.registry.issuers
    }
    previous_key_identities = {
        _decode_base64(
            str(item["public_key_base64"]),
            label="issuer public key",
            max_bytes=32,
        ): (str(item["issuer_id"]), str(item["key_id"]))
        for item in previous_raw_issuers
        if isinstance(item, Mapping)
    }
    for item in candidate_raw_issuers:
        if not isinstance(item, Mapping):
            raise JointRetentionRegistryError("registry issuer representation is invalid")
        identity = (str(item["issuer_id"]), str(item["key_id"]))
        previous_identity = previous_key_identities.get(
            _decode_base64(
                str(item["public_key_base64"]),
                label="issuer public key",
                max_bytes=32,
            )
        )
        if previous_identity is not None and previous_identity != identity:
            raise JointRetentionRegistryError(
                "joint-retention registry cannot rebind activated issuer key material"
            )
    for old_item in previous_raw_issuers:
        if not isinstance(old_item, Mapping):
            raise JointRetentionRegistryError("registry issuer representation is invalid")
        identity = (str(old_item["issuer_id"]), str(old_item["key_id"]))
        new_item = candidate_issuers.get(identity)
        if new_item is None:
            raise JointRetentionRegistryError(
                "joint-retention registry cannot remove an activated issuer key"
            )
        immutable_old = {key: value for key, value in old_item.items() if key != "revoked_at"}
        immutable_new = {key: value for key, value in new_item.items() if key != "revoked_at"}
        if immutable_old != immutable_new:
            raise JointRetentionRegistryError(
                "joint-retention registry cannot mutate an activated issuer key"
            )
        previous_revoked_at = previous_parsed_issuers[identity].revoked_at
        candidate_revoked_at = candidate_parsed_issuers[identity].revoked_at
        if previous_revoked_at is not None and (
            candidate_revoked_at is None or candidate_revoked_at > previous_revoked_at
        ):
            raise JointRetentionRegistryError(
                "joint-retention registry cannot clear or delay an issuer revocation"
            )

    for field in ("revoked_statement_sha256", "revoked_system_versions"):
        previous_values = previous.get(field)
        candidate_values = candidate.get(field)
        if not isinstance(previous_values, list) or not isinstance(candidate_values, list):
            raise JointRetentionRegistryError("registry revocation representation is invalid")
        if not set(previous_values).issubset(candidate_values):
            raise JointRetentionRegistryError(
                f"joint-retention registry cannot remove entries from {field}"
            )
    return True


def _dialect_name(executor: Session | Connection) -> str:
    if isinstance(executor, Session):
        return executor.get_bind().dialect.name
    return executor.dialect.name


def assert_joint_retention_registry_activated(
    executor: Session | Connection,
    registry: Mapping[str, Any],
    *,
    production: bool,
) -> int | None:
    """Assert one exact activated registry inside the caller's transaction."""

    if not production:
        # Non-production deliberately has no activation claim or persistent
        # high-water state.  The signature resolver still validates registry
        # and evidence contents independently.
        return None
    if _dialect_name(executor) != "postgresql":
        raise JointRetentionRegistryError(
            "production joint-retention registry activation requires PostgreSQL"
        )
    binding = joint_retention_registry_binding(registry)
    try:
        epoch = executor.scalar(
            text(
                "SELECT public.custombuild_joint_retention_assert_registry("
                ":canonical_json, :registry_sha256)"
            ),
            {
                "canonical_json": binding.canonical_json,
                "registry_sha256": binding.sha256,
            },
        )
    except SQLAlchemyError as exc:
        raise JointRetentionRegistryError(
            "production joint-retention trust registry is not activated exactly"
        ) from exc
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise JointRetentionRegistryError(
            "production joint-retention registry activation is invalid"
        )
    return int(epoch)
