"""Prepare, sign and verify exact joint-retention evidence v2.

This operator tool never creates a key and never accepts private-key material on
the command line, standard input or through an environment variable.  Signing
is a separate, explicitly confirmed command and reads one owner-only Ed25519
PEM file.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for _source_path in (
    _REPOSITORY_ROOT,
    _REPOSITORY_ROOT / "packages/domain/src",
    _REPOSITORY_ROOT / "services/api",
):
    _source = str(_source_path)
    if _source not in sys.path:
        sys.path.insert(0, _source)

from app.joint_retention import (  # noqa: E402
    JOINT_RETENTION_CERTIFIER_ROLE,
    MAX_EMBEDDED_DOCUMENT_BYTES,
    MAX_SIGNED_EVIDENCE_BYTES,
    SIGNED_EVIDENCE_SCHEMA_VERSION,
    TRUST_REGISTRY_SCHEMA_VERSION,
    JointRetentionTrustError,
    RetentionCatalogueEntry,
    SignedRetentionEvidence,
    TrustedIssuer,
    TrustRegistry,
    resolve_joint_retention_contract,
    validate_signed_retention_evidence_structure,
)
from app.joint_retention import (  # noqa: E402
    _canonical_json_bytes as server_canonical_json_bytes,
)
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from custombuild_domain import JointRetentionLoadMode  # noqa: E402
from custombuild_domain.models import (  # noqa: E402
    JointRetentionApplicationClass,
)
from pydantic import (  # noqa: E402
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

CERTIFICATION_REQUEST_SCHEMA_VERSION: Final = (
    "custombuild.joint-retention-certification-request.v2"
)
MAX_INPUT_JSON_BYTES: Final = 2 * 1024 * 1024
MAX_PASSWORD_BYTES: Final = 4096
_SHA256_PATTERN: Final = r"^[a-f0-9]{64}$"
_SAFE_KEY_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_UNSIGNED_FIELDS: Final = frozenset(SignedRetentionEvidence.model_fields) - {
    "signature_base64"
}


class CertifierToolkitError(RuntimeError):
    """The requested certifier operation is unsafe or inconsistent."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CertificationMaterial(_StrictModel):
    material_id: str = Field(pattern=_SAFE_KEY_PATTERN)
    material_version: str = Field(pattern=_SAFE_KEY_PATTERN)
    actual_thickness_um: StrictInt = Field(gt=0)


class CertificationLoadCase(_StrictModel):
    mode: Literal["shear", "withdrawal"]
    rated_design_load_n: StrictInt = Field(gt=0)


class ExcludedApplication(_StrictModel):
    application_class: Literal["captive_inset_back_groove", "surface_mounted_back"]
    joint_count: StrictInt = Field(ge=0)
    retention_basis: Literal[
        "canonical_four_boundary_geometric_capture",
        "independent_authenticated_evidence_required",
    ]
    capture_proven: StrictBool

    @model_validator(mode="after")
    def validate_semantics(self) -> ExcludedApplication:
        if self.application_class == "captive_inset_back_groove":
            if self.retention_basis != "canonical_four_boundary_geometric_capture":
                raise ValueError("captive inset-back exclusion has another retention basis")
        elif (
            self.retention_basis != "independent_authenticated_evidence_required"
            or self.capture_proven
        ):
            raise ValueError("surface-mounted back exclusion must remain unverified")
        return self


class CertificationRequest(_StrictModel):
    schema_version: Literal["custombuild.joint-retention-certification-request.v2"]
    signed_evidence_schema_version: Literal[
        "custombuild.joint-retention-signed-evidence.v2"
    ]
    application_class: Literal["load_bearing_carcass_dado"]
    joint_geometry_fingerprint_schema: Literal[
        "custombuild.joint-retention-application-geometry.v1"
    ]
    source_design_hash: str = Field(pattern=_SHA256_PATTERN)
    joint_geometry_sha256: str = Field(pattern=_SHA256_PATTERN)
    engine_version: str = Field(pattern=_SAFE_KEY_PATTERN, max_length=80)
    template_version: str = Field(pattern=_SAFE_KEY_PATTERN, max_length=80)
    eligible_for_current_binding: StrictBool
    blocking_issue: Literal["back_panel_capture_not_proven"] | None
    excluded_applications: tuple[ExcludedApplication, ...]
    required_materials: tuple[CertificationMaterial, ...] = Field(min_length=1)
    required_load_cases: tuple[CertificationLoadCase, CertificationLoadCase]
    minimum_safety_factor_permille: StrictInt = Field(ge=1_000, le=5_000)

    @field_validator("required_materials")
    @classmethod
    def validate_materials(
        cls,
        value: tuple[CertificationMaterial, ...],
    ) -> tuple[CertificationMaterial, ...]:
        keys = tuple((item.material_id, item.material_version) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("required materials must be sorted and unique")
        return value

    @field_validator("required_load_cases")
    @classmethod
    def validate_load_cases(
        cls,
        value: tuple[CertificationLoadCase, CertificationLoadCase],
    ) -> tuple[CertificationLoadCase, CertificationLoadCase]:
        if tuple(item.mode for item in value) != ("shear", "withdrawal"):
            raise ValueError("required load cases must be canonical shear then withdrawal")
        return value

    @model_validator(mode="after")
    def validate_eligibility(self) -> CertificationRequest:
        if self.eligible_for_current_binding != (self.blocking_issue is None):
            raise ValueError("certification request eligibility and blocker disagree")
        return self


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Use the exact JSON canonicalization used by the server verifier."""

    return server_canonical_json_bytes(value)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CertifierToolkitError("JSON contains a duplicate object key")
        result[key] = value
    return result


def _reject_unsafe_numbers(value: Any) -> None:
    if isinstance(value, float):
        raise CertifierToolkitError("JSON floating-point numbers are not accepted")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_unsafe_numbers(item)
    elif isinstance(value, list):
        for item in value:
            _reject_unsafe_numbers(item)


def _json_mapping(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        decoded = data.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
    except CertifierToolkitError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CertifierToolkitError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CertifierToolkitError(f"{label} must contain one JSON object")
    _reject_unsafe_numbers(value)
    return cast(dict[str, Any], value)


def _read_file(path: Path, *, label: str, maximum: int) -> bytes:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise CertifierToolkitError(f"{label} must be a regular non-symlink file")
        if metadata.st_size <= 0 or metadata.st_size > maximum:
            raise CertifierToolkitError(f"{label} is empty or exceeds its size limit")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size <= 0
            or opened.st_size > maximum
        ):
            raise CertifierToolkitError(f"{label} changed during secure open")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            content = stream.read(maximum + 1)
        if not content or len(content) > maximum:
            raise CertifierToolkitError(f"{label} is empty or exceeds its size limit")
        return content
    except CertifierToolkitError:
        raise
    except OSError as exc:
        raise CertifierToolkitError(f"could not read {label}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_json(path: Path, *, label: str, maximum: int = MAX_INPUT_JSON_BYTES) -> dict[str, Any]:
    return _json_mapping(_read_file(path, label=label, maximum=maximum), label=label)


def _write_new_file(path: Path, content: bytes) -> None:
    if not content:
        raise CertifierToolkitError("refusing to write an empty output")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CertifierToolkitError("output must be a new file in an existing directory") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        with suppress(OSError):
            path.unlink(missing_ok=True)
        raise CertifierToolkitError("could not write the output file") from exc


def _safe_key(value: str, *, label: str) -> str:
    if re.fullmatch(_SAFE_KEY_PATTERN, value) is None:
        raise CertifierToolkitError(f"{label} must be a safe stable identifier")
    return value


def _utc_timestamp(value: str, *, label: str) -> tuple[str, datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CertifierToolkitError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CertifierToolkitError(f"{label} must include a timezone")
    utc_value = parsed.astimezone(UTC)
    canonical = utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return canonical, utc_value


def load_request(path: Path) -> CertificationRequest:
    raw = _read_json(path, label="certification request")
    try:
        request = CertificationRequest.model_validate(raw)
    except ValidationError as exc:
        raise CertifierToolkitError(
            "certification request has an invalid or unknown field"
        ) from exc
    if not request.eligible_for_current_binding:
        raise CertifierToolkitError(
            "certification request is not eligible for the current retention binding"
        )
    return request


def load_registry(path: Path) -> TrustRegistry:
    raw = _read_json(path, label="trust registry")
    try:
        registry = TrustRegistry.model_validate(raw)
    except ValidationError as exc:
        raise CertifierToolkitError("trust registry has an invalid or unknown field") from exc
    issuer_keys = tuple((item.issuer_id, item.key_id) for item in registry.issuers)
    if issuer_keys != tuple(sorted(set(issuer_keys))):
        raise CertifierToolkitError("trust-registry issuers must be sorted and unique")
    for issuer in registry.issuers:
        _safe_key(issuer.issuer_id, label="issuer ID")
        _safe_key(issuer.key_id, label="key ID")
        if issuer.not_before >= issuer.not_after:
            raise CertifierToolkitError("issuer key validity interval is empty or inverted")
        try:
            public_key = base64.b64decode(issuer.public_key_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CertifierToolkitError("issuer public key is not canonical base64") from exc
        if (
            len(public_key) != 32
            or base64.b64encode(public_key).decode("ascii") != issuer.public_key_base64
        ):
            raise CertifierToolkitError("issuer public key must be 32-byte Ed25519 material")
    for item in registry.revoked_system_versions:
        if item.count("@") != 1:
            raise CertifierToolkitError("revoked system versions must use system@version")
        system_id, version = item.split("@")
        _safe_key(system_id, label="revoked system ID")
        _safe_key(version, label="revoked system version")
    return registry


def _issuer_for_payload(
    registry: TrustRegistry,
    payload: Mapping[str, Any],
    *,
    now: datetime,
) -> TrustedIssuer:
    issuer_id = str(payload.get("issuer_id", ""))
    key_id = str(payload.get("key_id", ""))
    issuer = next(
        (
            item
            for item in registry.issuers
            if item.issuer_id == issuer_id and item.key_id == key_id
        ),
        None,
    )
    if issuer is None or issuer.role != JOINT_RETENTION_CERTIFIER_ROLE:
        raise CertifierToolkitError("payload issuer/key is not an active certifier registry entry")
    envelope = SignedRetentionEvidence.model_validate(
        {**payload, "signature_base64": base64.b64encode(b"0" * 64).decode("ascii")}
    )
    issued_at = envelope.issued_at
    expires_at = envelope.expires_at
    if issuer.revoked_at is not None and issuer.revoked_at <= now:
        raise CertifierToolkitError("payload issuer key is revoked")
    if not issuer.not_before <= issued_at <= issuer.not_after:
        raise CertifierToolkitError("payload issuance is outside the issuer key validity")
    if not issuer.not_before <= now <= issuer.not_after:
        raise CertifierToolkitError("payload issuer key is not currently valid")
    if expires_at > issuer.not_after:
        raise CertifierToolkitError("payload expiry exceeds the issuer key validity")
    return issuer


def _validate_payload_shape(payload: Mapping[str, Any]) -> SignedRetentionEvidence:
    if set(payload) != _UNSIGNED_FIELDS:
        raise CertifierToolkitError("unsigned payload has a missing, unknown or signature field")
    dummy_signature = base64.b64encode(b"0" * 64).decode("ascii")
    candidate = {**payload, "signature_base64": dummy_signature}
    try:
        evidence = SignedRetentionEvidence.model_validate(candidate)
        validate_signed_retention_evidence_structure(canonical_json_bytes(candidate))
    except (ValidationError, JointRetentionTrustError) as exc:
        raise CertifierToolkitError("unsigned payload has an invalid or unsafe field") from exc
    raw_entry = payload.get("catalogue_entry")
    if not isinstance(raw_entry, Mapping) or canonical_json_bytes(
        cast(Mapping[str, Any], raw_entry)
    ) != canonical_json_bytes(evidence.catalogue_entry.model_dump(mode="json")):
        raise CertifierToolkitError("catalogue entry contains a coerced or unsafe value")
    for label, document in (
        ("test-report content", evidence.test_report.content_base64),
        (
            "installation-instruction content",
            evidence.installation_instruction.content_base64,
        ),
    ):
        try:
            decoded = base64.b64decode(document, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CertifierToolkitError(f"{label} is not canonical base64") from exc
        if base64.b64encode(decoded).decode("ascii") != document:
            raise CertifierToolkitError(f"{label} is not canonical base64")
    for label, value in (
        ("evidence ID", evidence.evidence_id),
        ("issuer ID", evidence.issuer_id),
        ("key ID", evidence.key_id),
        ("engine version", evidence.engine_version),
        ("template version", evidence.template_version),
        ("system ID", evidence.catalogue_entry.system_id),
        ("system version", evidence.catalogue_entry.system_version),
        ("hardware SKU", evidence.catalogue_entry.hardware_sku),
        ("test-report ID", evidence.test_report.document_id),
        ("test-report version", evidence.test_report.document_version),
        ("installation-instruction ID", evidence.installation_instruction.document_id),
        (
            "installation-instruction version",
            evidence.installation_instruction.document_version,
        ),
    ):
        _safe_key(value, label=label)
    for material in evidence.catalogue_entry.applicable_materials:
        _safe_key(material.material_id, label="applicable material ID")
        _safe_key(material.material_version, label="applicable material version")
    return evidence


def validate_payload_binding(
    payload: Mapping[str, Any],
    request: CertificationRequest,
    registry: TrustRegistry,
    *,
    now: datetime | None = None,
) -> SignedRetentionEvidence:
    current_time = datetime.now(UTC) if now is None else now.astimezone(UTC)
    evidence = _validate_payload_shape(payload)
    if (
        evidence.schema_version != request.signed_evidence_schema_version
        or evidence.application_class != request.application_class
        or evidence.joint_geometry_fingerprint_schema
        != request.joint_geometry_fingerprint_schema
        or evidence.joint_geometry_sha256 != request.joint_geometry_sha256
        or evidence.engine_version != request.engine_version
        or evidence.template_version != request.template_version
    ):
        raise CertifierToolkitError(
            "unsigned payload does not match the request geometry/compiler binding"
        )
    entry = evidence.catalogue_entry
    required_materials = {
        (item.material_id, item.material_version): item.actual_thickness_um
        for item in request.required_materials
    }
    covered_materials = {
        (item.material_id, item.material_version) for item in entry.applicable_materials
    }
    if not set(required_materials) <= covered_materials or any(
        not entry.minimum_applicable_thickness_um
        <= thickness
        <= entry.maximum_applicable_thickness_um
        for thickness in required_materials.values()
    ):
        raise CertifierToolkitError("unsigned payload does not cover the request materials")
    if tuple(item.mode for item in entry.load_cases) != tuple(
        item.mode for item in request.required_load_cases
    ) or any(
        payload_case.rated_design_load_n < request_case.rated_design_load_n
        for payload_case, request_case in zip(
            entry.load_cases,
            request.required_load_cases,
            strict=True,
        )
    ):
        raise CertifierToolkitError("unsigned payload is below the request load binding")
    if entry.safety_factor_permille < request.minimum_safety_factor_permille:
        raise CertifierToolkitError("unsigned payload is below the request safety factor")
    if any(
        load_case.verified_capacity_n * 1_000
        < load_case.rated_design_load_n * entry.safety_factor_permille
        for load_case in entry.load_cases
    ):
        raise CertifierToolkitError(
            "unsigned payload capacity is below its rated load and safety factor"
        )
    if evidence.issued_at > current_time:
        raise CertifierToolkitError("unsigned payload was issued in the future")
    if evidence.expires_at <= evidence.issued_at or evidence.expires_at <= current_time:
        raise CertifierToolkitError("unsigned payload is expired or has an invalid interval")
    _issuer_for_payload(registry, payload, now=current_time)
    system_key = f"{entry.system_id}@{entry.system_version}"
    if system_key in registry.revoked_system_versions:
        raise CertifierToolkitError("unsigned payload selects a revoked retention system")
    return evidence


def prepare_payload(
    *,
    request: CertificationRequest,
    registry: TrustRegistry,
    catalogue_entry: Mapping[str, Any],
    evidence_id: str,
    issuer_id: str,
    key_id: str,
    issued_at: str,
    expires_at: str,
    test_report: bytes,
    test_report_id: str,
    test_report_version: str,
    installation_instruction: bytes,
    installation_instruction_id: str,
    installation_instruction_version: str,
    now: datetime | None = None,
) -> bytes:
    try:
        entry = RetentionCatalogueEntry.model_validate(catalogue_entry)
    except ValidationError as exc:
        raise CertifierToolkitError("catalogue entry has an invalid or unknown field") from exc
    if canonical_json_bytes(catalogue_entry) != canonical_json_bytes(
        entry.model_dump(mode="json")
    ):
        raise CertifierToolkitError("catalogue entry contains a coerced or unsafe value")
    issued_text, _ = _utc_timestamp(issued_at, label="issued-at")
    expiry_text, _ = _utc_timestamp(expires_at, label="expires-at")

    def document(document_id: str, version: str, content: bytes) -> dict[str, Any]:
        if not content or len(content) > MAX_EMBEDDED_DOCUMENT_BYTES:
            raise CertifierToolkitError("embedded document is empty or exceeds its size limit")
        return {
            "document_id": _safe_key(document_id, label="document ID"),
            "document_version": _safe_key(version, label="document version"),
            "sha256": hashlib.sha256(content).hexdigest(),
            "content_base64": base64.b64encode(content).decode("ascii"),
        }

    payload: dict[str, Any] = {
        "schema_version": SIGNED_EVIDENCE_SCHEMA_VERSION,
        "evidence_id": _safe_key(evidence_id, label="evidence ID"),
        "issuer_id": _safe_key(issuer_id, label="issuer ID"),
        "key_id": _safe_key(key_id, label="key ID"),
        "issued_at": issued_text,
        "expires_at": expiry_text,
        "application_class": request.application_class,
        "joint_geometry_fingerprint_schema": request.joint_geometry_fingerprint_schema,
        "engine_version": request.engine_version,
        "template_version": request.template_version,
        "joint_geometry_sha256": request.joint_geometry_sha256,
        "catalogue_entry": entry.model_dump(mode="json"),
        "test_report": document(test_report_id, test_report_version, test_report),
        "installation_instruction": document(
            installation_instruction_id,
            installation_instruction_version,
            installation_instruction,
        ),
    }
    validate_payload_binding(payload, request, registry, now=now)
    encoded = canonical_json_bytes(payload)
    # The signature field adds only a bounded constant, but use the real
    # envelope shape so this guard cannot drift from server storage limits.
    projected = canonical_json_bytes(
        {**payload, "signature_base64": base64.b64encode(b"0" * 64).decode("ascii")}
    )
    if len(projected) > MAX_SIGNED_EVIDENCE_BYTES:
        raise CertifierToolkitError("signed evidence would exceed the server size limit")
    return encoded


def _read_secret_file(path: Path, *, label: str, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CertifierToolkitError(f"could not inspect {label}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CertifierToolkitError(f"{label} must be a regular non-symlink file")
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise CertifierToolkitError(f"{label} must be owner-owned with mode 0600 or stricter")
    if metadata.st_size <= 0 or metadata.st_size > maximum:
        raise CertifierToolkitError(f"{label} is empty or exceeds its size limit")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_mode & 0o077
            or opened.st_size <= 0
            or opened.st_size > maximum
        ):
            raise CertifierToolkitError(f"{label} changed during secure open")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise CertifierToolkitError(f"{label} exceeds its size limit")
        return content
    except CertifierToolkitError:
        raise
    except OSError as exc:
        raise CertifierToolkitError(f"could not read {label}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _load_private_key(key_path: Path, password_path: Path | None) -> Ed25519PrivateKey:
    key_bytes = _read_secret_file(key_path, label="private-key file", maximum=65_536)
    password: bytes | None = None
    if password_path is not None:
        password = _read_secret_file(
            password_path,
            label="private-key password file",
            maximum=MAX_PASSWORD_BYTES,
        ).removesuffix(b"\r\n").removesuffix(b"\n")
        if not password or b"\x00" in password or b"\n" in password or b"\r" in password:
            raise CertifierToolkitError("private-key password file has an unsafe value")
    try:
        loaded = serialization.load_pem_private_key(key_bytes, password=password)
    except (TypeError, ValueError) as exc:
        raise CertifierToolkitError("private-key file is not a usable encrypted/PEM key") from exc
    if not isinstance(loaded, Ed25519PrivateKey):
        raise CertifierToolkitError("private-key file must contain an Ed25519 private key")
    return loaded


def sign_payload(
    *,
    payload_bytes: bytes,
    request: CertificationRequest,
    registry: TrustRegistry,
    private_key_path: Path,
    password_path: Path | None = None,
    now: datetime | None = None,
) -> bytes:
    payload = _json_mapping(payload_bytes, label="unsigned payload")
    if payload_bytes != canonical_json_bytes(payload):
        raise CertifierToolkitError("unsigned payload must use canonical JSON bytes")
    validate_payload_binding(payload, request, registry, now=now)
    issuer = _issuer_for_payload(
        registry,
        payload,
        now=datetime.now(UTC) if now is None else now.astimezone(UTC),
    )
    private_key = _load_private_key(private_key_path, password_path)
    derived_public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    registered_public = base64.b64decode(issuer.public_key_base64, validate=True)
    if not hmac.compare_digest(derived_public, registered_public):
        raise CertifierToolkitError("private key does not match the selected registry entry")
    signature = private_key.sign(payload_bytes)
    evidence_bytes = canonical_json_bytes(
        {**payload, "signature_base64": base64.b64encode(signature).decode("ascii")}
    )
    if len(evidence_bytes) > MAX_SIGNED_EVIDENCE_BYTES:
        raise CertifierToolkitError("signed evidence exceeds the server size limit")
    # Do not emit bytes that the authoritative resolver would reject (including
    # an already revoked deterministic statement digest).
    verify_evidence(
        evidence_bytes=evidence_bytes,
        request=request,
        registry=registry,
        now=now,
    )
    return evidence_bytes


def verify_evidence(
    *,
    evidence_bytes: bytes,
    request: CertificationRequest,
    registry: TrustRegistry,
    now: datetime | None = None,
) -> dict[str, Any]:
    raw = _json_mapping(evidence_bytes, label="signed evidence")
    if evidence_bytes != canonical_json_bytes(raw):
        raise CertifierToolkitError("signed evidence must use canonical JSON bytes")
    if set(raw) != set(_UNSIGNED_FIELDS) | {"signature_base64"}:
        raise CertifierToolkitError("signed evidence has a missing or unknown field")
    raw_signature = raw.get("signature_base64")
    if not isinstance(raw_signature, str):
        raise CertifierToolkitError("signed evidence signature is not canonical base64")
    try:
        decoded_signature = base64.b64decode(raw_signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CertifierToolkitError("signed evidence signature is not canonical base64") from exc
    if (
        len(decoded_signature) != 64
        or base64.b64encode(decoded_signature).decode("ascii") != raw_signature
    ):
        raise CertifierToolkitError("signed evidence signature is not canonical base64")
    unsigned = dict(raw)
    unsigned.pop("signature_base64")
    evidence = validate_payload_binding(unsigned, request, registry, now=now)
    try:
        contract = resolve_joint_retention_contract(
            trust_registry=registry.model_dump(mode="json"),
            evidence_bytes=evidence_bytes,
            expected_application_class=(
                JointRetentionApplicationClass.LOAD_BEARING_CARCASS_DADO
            ),
            expected_joint_geometry_sha256=request.joint_geometry_sha256,
            expected_engine_version=request.engine_version,
            expected_template_version=request.template_version,
            required_materials=tuple(
                (item.material_id, item.material_version)
                for item in request.required_materials
            ),
            required_thicknesses_um=tuple(
                item.actual_thickness_um for item in request.required_materials
            ),
            required_loads_n=tuple(
                (JointRetentionLoadMode(item.mode), item.rated_design_load_n)
                for item in request.required_load_cases
            ),
            minimum_safety_factor_permille=request.minimum_safety_factor_permille,
            now=now,
        )
    except JointRetentionTrustError as exc:
        raise CertifierToolkitError(f"server verifier rejected evidence: {exc}") from exc
    return {
        "status": "evidence_verified_for_request",
        "schema_version": evidence.schema_version,
        # The statement authenticates the exact geometry/compiler/material/load
        # binding.  The source hash is request context and is deliberately not
        # presented as a field authenticated by the certifier signature.
        "request_source_design_hash": request.source_design_hash,
        "source_design_hash_authenticated_by_signature": False,
        "joint_geometry_sha256": request.joint_geometry_sha256,
        "evidence_id": contract.evidence_id,
        "evidence_sha256": contract.evidence_sha256,
        "issuer_id": evidence.issuer_id,
        "key_id": evidence.key_id,
        "system_id": contract.system_id,
        "system_version": contract.system_version,
        "expires_at": evidence.expires_at.isoformat(),
        "physical_cutting_authorized": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    registry = commands.add_parser("validate-registry", help="validate explicit registry JSON")
    registry.add_argument("--registry", required=True, type=Path)

    prepare = commands.add_parser("prepare", help="create one canonical unsigned payload")
    prepare.add_argument("--request", required=True, type=Path)
    prepare.add_argument("--registry", required=True, type=Path)
    prepare.add_argument("--catalogue-entry", required=True, type=Path)
    prepare.add_argument("--evidence-id", required=True)
    prepare.add_argument("--issuer-id", required=True)
    prepare.add_argument("--key-id", required=True)
    prepare.add_argument("--issued-at", required=True)
    prepare.add_argument("--expires-at", required=True)
    prepare.add_argument("--test-report", required=True, type=Path)
    prepare.add_argument("--test-report-id", required=True)
    prepare.add_argument("--test-report-version", required=True)
    prepare.add_argument("--installation-instruction", required=True, type=Path)
    prepare.add_argument("--installation-instruction-id", required=True)
    prepare.add_argument("--installation-instruction-version", required=True)
    prepare.add_argument("--output", required=True, type=Path)

    validate = commands.add_parser(
        "validate-payload",
        help="validate one canonical unsigned payload against its exact request",
    )
    validate.add_argument("--request", required=True, type=Path)
    validate.add_argument("--registry", required=True, type=Path)
    validate.add_argument("--payload", required=True, type=Path)

    sign = commands.add_parser("sign", help="explicitly sign one validated payload")
    sign.add_argument("--request", required=True, type=Path)
    sign.add_argument("--registry", required=True, type=Path)
    sign.add_argument("--payload", required=True, type=Path)
    sign.add_argument("--private-key-file", required=True, type=Path)
    sign.add_argument("--private-key-password-file", type=Path)
    sign.add_argument("--output", required=True, type=Path)
    sign.add_argument("--confirm-signing", action="store_true", required=True)

    verify = commands.add_parser("verify", help="run the exact server verifier")
    verify.add_argument("--request", required=True, type=Path)
    verify.add_argument("--registry", required=True, type=Path)
    verify.add_argument("--evidence", required=True, type=Path)
    verify.add_argument(
        "--at",
        help="explicit verification timestamp for reproducible audits (defaults to now)",
    )
    return parser


def _summary(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate-registry":
            registry = load_registry(arguments.registry)
            canonical = canonical_json_bytes(
                _read_json(arguments.registry, label="trust registry")
            )
            _summary(
                {
                    "status": "valid",
                    "schema_version": TRUST_REGISTRY_SCHEMA_VERSION,
                    "issuer_count": len(registry.issuers),
                    "registry_sha256": hashlib.sha256(canonical).hexdigest(),
                }
            )
            return 0

        request = load_request(arguments.request)
        registry = load_registry(arguments.registry)
        if arguments.command == "prepare":
            content = prepare_payload(
                request=request,
                registry=registry,
                catalogue_entry=_read_json(
                    arguments.catalogue_entry,
                    label="retention catalogue entry",
                ),
                evidence_id=arguments.evidence_id,
                issuer_id=arguments.issuer_id,
                key_id=arguments.key_id,
                issued_at=arguments.issued_at,
                expires_at=arguments.expires_at,
                test_report=_read_file(
                    arguments.test_report,
                    label="retention test report",
                    maximum=MAX_EMBEDDED_DOCUMENT_BYTES,
                ),
                test_report_id=arguments.test_report_id,
                test_report_version=arguments.test_report_version,
                installation_instruction=_read_file(
                    arguments.installation_instruction,
                    label="installation instruction",
                    maximum=MAX_EMBEDDED_DOCUMENT_BYTES,
                ),
                installation_instruction_id=arguments.installation_instruction_id,
                installation_instruction_version=(
                    arguments.installation_instruction_version
                ),
            )
            _write_new_file(arguments.output, content)
            _summary(
                {
                    "status": "prepared",
                    "output_sha256": hashlib.sha256(content).hexdigest(),
                    "signed": False,
                }
            )
            return 0
        if arguments.command == "validate-payload":
            content = _read_file(
                arguments.payload,
                label="unsigned payload",
                maximum=MAX_SIGNED_EVIDENCE_BYTES,
            )
            raw = _json_mapping(content, label="unsigned payload")
            if content != canonical_json_bytes(raw):
                raise CertifierToolkitError("unsigned payload must use canonical JSON bytes")
            validate_payload_binding(raw, request, registry)
            _summary(
                {
                    "status": "valid",
                    "payload_sha256": hashlib.sha256(content).hexdigest(),
                    "signed": False,
                }
            )
            return 0
        if arguments.command == "sign":
            content = _read_file(
                arguments.payload,
                label="unsigned payload",
                maximum=MAX_SIGNED_EVIDENCE_BYTES,
            )
            if arguments.output.resolve(strict=False) == arguments.private_key_file.resolve():
                raise CertifierToolkitError("output must not replace the private-key file")
            signed = sign_payload(
                payload_bytes=content,
                request=request,
                registry=registry,
                private_key_path=arguments.private_key_file,
                password_path=arguments.private_key_password_file,
            )
            _write_new_file(arguments.output, signed)
            _summary(
                {
                    "status": "signed",
                    "evidence_sha256": hashlib.sha256(signed).hexdigest(),
                }
            )
            return 0
        if arguments.command == "verify":
            verification_time = None
            if arguments.at:
                _, verification_time = _utc_timestamp(arguments.at, label="verification time")
            evidence = _read_file(
                arguments.evidence,
                label="signed evidence",
                maximum=MAX_SIGNED_EVIDENCE_BYTES,
            )
            _summary(
                verify_evidence(
                    evidence_bytes=evidence,
                    request=request,
                    registry=registry,
                    now=verification_time,
                )
            )
            return 0
        raise CertifierToolkitError("unknown command")
    except CertifierToolkitError as exc:
        print(f"certifier toolkit refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
