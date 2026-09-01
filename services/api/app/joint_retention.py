"""Authenticated server-side trust boundary for joint-retention evidence.

Clients may select an immutable evidence object, but they can never construct a
``JointRetentionContract``.  This module verifies an Ed25519-signed statement,
the server-owned issuer registry, revocation state and the exact embedded test
report and installation-instruction bytes before deriving the frozen domain
contract.  No trusted issuer or evidence is bundled with the application.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from custombuild_domain import (
    JointRetentionContract,
    JointRetentionLoadCase,
    JointRetentionLoadMode,
    JointRetentionMachiningScope,
    JointRetentionMaterialIdentity,
    JointRetentionMethod,
    JointType,
    content_hash,
)
from custombuild_domain.models import JointRetentionApplicationClass
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

TRUST_REGISTRY_SCHEMA_VERSION = "custombuild.joint-retention-trust-registry.v1"
SIGNED_EVIDENCE_SCHEMA_VERSION = "custombuild.joint-retention-signed-evidence.v2"
JOINT_GEOMETRY_FINGERPRINT_SCHEMA = (
    "custombuild.joint-retention-application-geometry.v1"
)
JOINT_RETENTION_CERTIFIER_ROLE = "joint_retention_certifier"
MAX_SIGNED_EVIDENCE_BYTES = 20 * 1024 * 1024
MAX_EMBEDDED_DOCUMENT_BYTES = 8 * 1024 * 1024


class JointRetentionTrustError(ValueError):
    """Raised when retention evidence cannot cross the server trust boundary."""


class _StrictModel(BaseModel):
    # JSON arrays must be accepted for immutable tuple fields.  Individual
    # numeric fields still carry explicit bounds and booleans are rejected by
    # the domain model before a contract can be returned.
    model_config = ConfigDict(extra="forbid", frozen=True)


class TrustedIssuer(_StrictModel):
    issuer_id: str = Field(min_length=1, max_length=160)
    key_id: str = Field(min_length=1, max_length=160)
    role: Literal["joint_retention_certifier"]
    public_key_base64: str = Field(min_length=1, max_length=128)
    not_before: datetime
    not_after: datetime
    revoked_at: datetime | None = None

    @field_validator("not_before", "not_after", "revoked_at")
    @classmethod
    def require_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("issuer timestamps must include a timezone")
        return value.astimezone(UTC)


class TrustRegistry(_StrictModel):
    schema_version: Literal["custombuild.joint-retention-trust-registry.v1"]
    issuers: tuple[TrustedIssuer, ...]
    revoked_statement_sha256: tuple[str, ...] = ()
    revoked_system_versions: tuple[str, ...] = ()

    @field_validator("revoked_statement_sha256")
    @classmethod
    def validate_revoked_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(not _is_sha256(item) for item in value):
            raise ValueError("revoked statement digests must be sorted unique SHA-256 values")
        return value

    @field_validator("revoked_system_versions")
    @classmethod
    def validate_revoked_systems(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item or "@" not in item or item != item.strip() for item in value
        ):
            raise ValueError("revoked system versions must be sorted unique system@version values")
        return value


class EmbeddedDocument(_StrictModel):
    document_id: str = Field(min_length=1, max_length=160)
    document_version: str = Field(min_length=1, max_length=80)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_base64: str = Field(min_length=1)


class RetentionMaterial(_StrictModel):
    material_id: str = Field(min_length=1, max_length=160)
    material_version: str = Field(min_length=1, max_length=80)


class RetentionLoadCase(_StrictModel):
    mode: Literal["shear", "withdrawal"]
    rated_design_load_n: int = Field(gt=0)
    verified_capacity_n: int = Field(gt=0)


class RetentionCatalogueEntry(_StrictModel):
    system_id: str = Field(min_length=1, max_length=160)
    system_version: str = Field(min_length=1, max_length=80)
    joint_type: Literal["dado"]
    application_class: Literal["load_bearing_carcass_dado"]
    method: Literal["mechanical"]
    machining_scope: Literal["no_additional_cnc"]
    hardware_sku: str = Field(min_length=1, max_length=160)
    hardware_count_per_joint: int = Field(gt=0, le=100)
    applicable_materials: tuple[RetentionMaterial, ...] = Field(min_length=1)
    minimum_applicable_thickness_um: int = Field(gt=0)
    maximum_applicable_thickness_um: int = Field(gt=0)
    load_cases: tuple[RetentionLoadCase, RetentionLoadCase]
    safety_factor_permille: int = Field(ge=1_000, le=10_000)

    @field_validator("applicable_materials")
    @classmethod
    def validate_materials(
        cls, value: tuple[RetentionMaterial, ...]
    ) -> tuple[RetentionMaterial, ...]:
        keys = tuple((item.material_id, item.material_version) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("applicable materials must be sorted and unique")
        return value

    @field_validator("load_cases")
    @classmethod
    def validate_load_cases(
        cls, value: tuple[RetentionLoadCase, RetentionLoadCase]
    ) -> tuple[RetentionLoadCase, RetentionLoadCase]:
        if tuple(item.mode for item in value) != ("shear", "withdrawal"):
            raise ValueError("load cases must contain canonical shear and withdrawal entries")
        return value


class SignedRetentionEvidence(_StrictModel):
    schema_version: Literal["custombuild.joint-retention-signed-evidence.v2"]
    evidence_id: str = Field(min_length=1, max_length=160)
    issuer_id: str = Field(min_length=1, max_length=160)
    key_id: str = Field(min_length=1, max_length=160)
    issued_at: datetime
    expires_at: datetime
    application_class: Literal["load_bearing_carcass_dado"]
    joint_geometry_fingerprint_schema: Literal[
        "custombuild.joint-retention-application-geometry.v1"
    ]
    engine_version: str = Field(min_length=1, max_length=80)
    template_version: str = Field(min_length=1, max_length=80)
    joint_geometry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    catalogue_entry: RetentionCatalogueEntry
    test_report: EmbeddedDocument
    installation_instruction: EmbeddedDocument
    signature_base64: str = Field(min_length=1, max_length=256)

    @field_validator("issued_at", "expires_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence timestamps must include a timezone")
        return value.astimezone(UTC)


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise JointRetentionTrustError("retention evidence is not canonical JSON data") from exc


def _parse_json_mapping(data: bytes, *, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JointRetentionTrustError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise JointRetentionTrustError(f"{label} must contain a JSON object")
    return value


def _decode_base64(value: str, *, label: str, max_bytes: int) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise JointRetentionTrustError(f"{label} is not canonical base64") from exc
    if not decoded or len(decoded) > max_bytes:
        raise JointRetentionTrustError(f"{label} is empty or exceeds its size limit")
    return decoded


def _load_registry(value: Mapping[str, Any]) -> TrustRegistry:
    try:
        registry = TrustRegistry.model_validate(value)
    except ValidationError as exc:
        raise JointRetentionTrustError("joint-retention trust registry is invalid") from exc
    issuer_keys = tuple((item.issuer_id, item.key_id) for item in registry.issuers)
    if issuer_keys != tuple(sorted(set(issuer_keys))):
        raise JointRetentionTrustError("trusted issuers must be sorted and unique")
    return registry


def _load_evidence(evidence_bytes: bytes) -> tuple[SignedRetentionEvidence, Mapping[str, Any]]:
    if not evidence_bytes or len(evidence_bytes) > MAX_SIGNED_EVIDENCE_BYTES:
        raise JointRetentionTrustError("signed retention evidence exceeds its size limit")
    raw = _parse_json_mapping(evidence_bytes, label="signed retention evidence")
    if evidence_bytes != _canonical_json_bytes(raw):
        raise JointRetentionTrustError("signed retention evidence must use canonical JSON bytes")
    try:
        evidence = SignedRetentionEvidence.model_validate(raw)
    except ValidationError as exc:
        raise JointRetentionTrustError("signed retention evidence has an invalid schema") from exc
    return evidence, raw


def _find_trusted_issuer(
    registry: TrustRegistry,
    evidence: SignedRetentionEvidence,
    *,
    now: datetime,
) -> TrustedIssuer:
    issuer = next(
        (
            item
            for item in registry.issuers
            if item.issuer_id == evidence.issuer_id and item.key_id == evidence.key_id
        ),
        None,
    )
    if issuer is None or issuer.role != JOINT_RETENTION_CERTIFIER_ROLE:
        raise JointRetentionTrustError("retention evidence issuer is not trusted for certification")
    if issuer.revoked_at is not None and issuer.revoked_at <= now:
        raise JointRetentionTrustError("retention evidence issuer key is revoked")
    if not issuer.not_before <= evidence.issued_at <= issuer.not_after:
        raise JointRetentionTrustError("retention evidence was issued outside key validity")
    if not issuer.not_before <= now <= issuer.not_after:
        raise JointRetentionTrustError("retention evidence issuer key is not currently valid")
    return issuer


def _verify_signature(
    issuer: TrustedIssuer,
    evidence: SignedRetentionEvidence,
    raw: Mapping[str, Any],
) -> None:
    public_key_bytes = _decode_base64(
        issuer.public_key_base64,
        label="issuer public key",
        max_bytes=32,
    )
    if len(public_key_bytes) != 32:
        raise JointRetentionTrustError("issuer public key must be 32-byte Ed25519 material")
    signature = _decode_base64(
        evidence.signature_base64,
        label="retention evidence signature",
        max_bytes=64,
    )
    if len(signature) != 64:
        raise JointRetentionTrustError("retention evidence signature must be 64 bytes")
    signed_payload = dict(raw)
    signed_payload.pop("signature_base64", None)
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
            signature,
            _canonical_json_bytes(signed_payload),
        )
    except (InvalidSignature, ValueError) as exc:
        raise JointRetentionTrustError("retention evidence signature is invalid") from exc


def _verify_document(document: EmbeddedDocument, *, label: str) -> bytes:
    content = _decode_base64(
        document.content_base64,
        label=f"{label} content",
        max_bytes=MAX_EMBEDDED_DOCUMENT_BYTES,
    )
    if not _is_sha256(document.sha256) or hashlib.sha256(content).hexdigest() != document.sha256:
        raise JointRetentionTrustError(f"{label} checksum does not match its exact bytes")
    return content


def validate_signed_retention_evidence_structure(evidence_bytes: bytes) -> None:
    """Reject malformed envelopes before immutable storage is allocated.

    Authenticity and applicability are intentionally checked again when a
    server-owned trust registry resolves the evidence for an exact design.
    """

    evidence, _ = _load_evidence(evidence_bytes)
    _verify_document(evidence.test_report, label="retention test report")
    _verify_document(evidence.installation_instruction, label="installation instruction")


def resolve_joint_retention_contract(
    *,
    trust_registry: Mapping[str, Any],
    evidence_bytes: bytes,
    expected_application_class: JointRetentionApplicationClass,
    expected_joint_geometry_sha256: str,
    expected_engine_version: str,
    expected_template_version: str,
    required_materials: Sequence[tuple[str, str]],
    required_thicknesses_um: Sequence[int],
    now: datetime | None = None,
) -> JointRetentionContract:
    """Derive a domain contract only from authenticated, applicable evidence."""

    current_time = datetime.now(UTC) if now is None else now.astimezone(UTC)
    if (
        expected_application_class
        != JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO
    ):
        raise JointRetentionTrustError(
            "the current resolver only supports load-bearing carcass DADO evidence"
        )
    if not _is_sha256(expected_joint_geometry_sha256):
        raise JointRetentionTrustError("expected joint geometry must be a SHA-256 digest")
    if not expected_engine_version or not expected_template_version:
        raise JointRetentionTrustError("expected compiler versions must be non-blank")
    material_keys = tuple(sorted(set(required_materials)))
    if not material_keys or len(material_keys) != len(tuple(required_materials)):
        raise JointRetentionTrustError("required materials must be non-empty and unique")
    thicknesses = tuple(required_thicknesses_um)
    if not thicknesses or any(type(value) is not int or value <= 0 for value in thicknesses):
        raise JointRetentionTrustError("required thicknesses must be positive integers")

    registry = _load_registry(trust_registry)
    evidence, raw = _load_evidence(evidence_bytes)
    statement_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
    if statement_sha256 in registry.revoked_statement_sha256:
        raise JointRetentionTrustError("retention evidence statement is revoked")
    system_key = f"{evidence.catalogue_entry.system_id}@{evidence.catalogue_entry.system_version}"
    if system_key in registry.revoked_system_versions:
        raise JointRetentionTrustError("retention system version is revoked")
    if evidence.issued_at > current_time:
        raise JointRetentionTrustError("retention evidence was issued in the future")
    if evidence.expires_at <= evidence.issued_at or evidence.expires_at <= current_time:
        raise JointRetentionTrustError("retention evidence is expired")

    issuer = _find_trusted_issuer(registry, evidence, now=current_time)
    _verify_signature(issuer, evidence, raw)
    if (
        evidence.application_class != expected_application_class.value
        or evidence.catalogue_entry.application_class != expected_application_class.value
        or evidence.joint_geometry_sha256 != expected_joint_geometry_sha256
        or evidence.engine_version != expected_engine_version
        or evidence.template_version != expected_template_version
    ):
        raise JointRetentionTrustError(
            "retention evidence is bound to another application, geometry or compiler version"
        )
    _verify_document(evidence.test_report, label="retention test report")
    _verify_document(evidence.installation_instruction, label="installation instruction")

    entry = evidence.catalogue_entry
    covered_materials = {
        (item.material_id, item.material_version) for item in entry.applicable_materials
    }
    if not set(material_keys) <= covered_materials:
        raise JointRetentionTrustError("retention evidence does not cover every material version")
    if entry.minimum_applicable_thickness_um > entry.maximum_applicable_thickness_um or any(
        not entry.minimum_applicable_thickness_um
        <= thickness
        <= entry.maximum_applicable_thickness_um
        for thickness in thicknesses
    ):
        raise JointRetentionTrustError("retention evidence does not cover every member thickness")

    try:
        return JointRetentionContract(
            system_id=entry.system_id,
            system_version=entry.system_version,
            joint_type=JointType.DADO,
            application_class=expected_application_class,
            method=JointRetentionMethod.MECHANICAL,
            catalog_entry_sha256=content_hash(entry.model_dump(mode="python")),
            evidence_id=evidence.evidence_id,
            evidence_sha256=statement_sha256,
            installation_instruction_id=evidence.installation_instruction.document_id,
            installation_instruction_version=(evidence.installation_instruction.document_version),
            installation_instruction_sha256=evidence.installation_instruction.sha256,
            machining_scope=JointRetentionMachiningScope.NO_ADDITIONAL_CNC,
            hardware_sku=entry.hardware_sku,
            hardware_count_per_joint=entry.hardware_count_per_joint,
            applicable_materials=tuple(
                JointRetentionMaterialIdentity(
                    material_id=item.material_id,
                    material_version=item.material_version,
                )
                for item in entry.applicable_materials
            ),
            joint_geometry_sha256=expected_joint_geometry_sha256,
            minimum_applicable_thickness_um=entry.minimum_applicable_thickness_um,
            maximum_applicable_thickness_um=entry.maximum_applicable_thickness_um,
            load_cases=tuple(
                JointRetentionLoadCase(
                    mode=JointRetentionLoadMode(item.mode),
                    rated_design_load_n=item.rated_design_load_n,
                    verified_capacity_n=item.verified_capacity_n,
                )
                for item in entry.load_cases
            ),
            safety_factor_permille=entry.safety_factor_permille,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise JointRetentionTrustError(
            "authenticated retention evidence failed domain validation"
        ) from exc
